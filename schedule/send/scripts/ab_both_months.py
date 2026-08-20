"""TWO-MONTH A/B GATE -- the anti-overfitting rule, made executable.

    PYTHONPATH=. python scripts/ab_both_months.py PLANNER_WARM_PRESS=1
    PYTHONPATH=. python scripts/ab_both_months.py PLANNER_T0_STOCK_BASIS=lot PLANNER_L7_MAKEROOM=0

Runs the candidate flag set against the CURRENT DEFAULTS on both 2026-07 and
2026-08, prints the per-plant delta on each, and returns

    exit 0  ADOPT    -- non-negative on both months, positive on at least one
    exit 1  REJECT   -- negative on either month
    exit 2  NEUTRAL  -- no movement anywhere

WHY THIS SCRIPT EXISTS
  On 2026-08-13 two flags were found to have been adopted on a July-only A/B:

      PLANNER_WARM_PRESS=1        July PCR +0.6   August PCR -0.5
      PLANNER_T0_STOCK_BASIS=lot  July  ~0        August TBR -0.8, -2 L11

  Both read as improvements because only July was ever measured. Together they
  cost the shipped August pack 0.7 pt of combined fulfilment while making July
  look 0.4 pt better than the engine actually generalises to.

  The failure was not the flags. It was measuring one month and shipping. A
  single month cannot distinguish "this helps" from "this fits July", because
  the two months differ in the ways that matter most to this engine:

      July    demand IS the plant's own July production -> 100 % achievable,
              no partition (none exists), 48 demanded PCR GTs
      August  demand is a forward order book -> arithmetically infeasible by
              ~3.2 pt (presses peak 13,319/day vs 13,764 required), partitioned,
              73 demanded PCR GTs, tighter TBR allowable matrix

  A flag tuned on July is tuned on the easy, unpartitioned, feasible month.

THE RULE THIS ENFORCES
  A change ships only if it is NON-NEGATIVE ON BOTH MONTHS, per plant.
  "Net positive across the two" is NOT sufficient -- a +0.6/-0.5 flag is noise
  with a sign, and shipping it degrades a real pack to flatter a backtest.

  Tolerance is 0.05 pt: anything smaller is run-to-run formatting, not signal.

CAVEAT
  Two months is the floor, not the goal. It catches month-fitting; it cannot
  prove generalisation. When a third month's demand and opening GT land, add it
  to MONTHS below -- the gate arithmetic already handles any number.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = ROOT / ".venv" / "bin" / "python"

# Per-month environment that is NOT under test -- the month's own inputs and the
# two settled shape flags. Kept here so an arm cannot silently differ on them.
MONTHS: dict[str, dict[str, str]] = {
    "2026-07": {
        # STALE, CORRECTED 2026-08-18: a July partition exists and is stamped
        # 2026-07 (`cpsat_partition.py` builds from committed masters, no MES).
        # Forcing "" here silently ran every July arm on a NON-DEFAULT engine --
        # the shipped default is "PCR" -- so nothing measured through this driver
        # was measuring the shipped configuration. Left empty only if you are
        # deliberately reproducing a pre-2026-08-17 result.
        "PLANNER_PARTITION_PLANTS": "PCR",
        "PLANNER_LOT_INTERVAL_H": "8",
        "PLANNER_TH_GT_WIP_RAIL_MARGIN": "1.0",
    },
    "2026-08": {
        "PLANNER_OPENING_GT": "opening_gt_manual_2026-08.parquet",
        "PLANNER_LOT_INTERVAL_H": "8",
        "PLANNER_TH_GT_WIP_RAIL_MARGIN": "1.0",
    },
}

TOL = 0.05          # pt -- below this is noise, not a result


def _plan(month: str, run: str, extra: dict[str, str]) -> Path:
    env = dict(os.environ)
    env.update(MONTHS[month])
    env.update(extra)
    env["PYTHONPATH"] = "."
    env["PYTHONIOENCODING"] = "utf-8"
    out = Path(tempfile.mkdtemp(prefix=f"ab_{run}_"))
    r = subprocess.run(
        [str(PY), "main.py", "plan", "--month", month, "--run", run,
         "--out", str(out), "--btp-only"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-2000:])
        raise SystemExit(f"{run}: plan failed for {month}")
    return ROOT / "runs" / run


def _score(rd: Path) -> dict:
    """In-month fulfilment per plant + the L11 pass count.

    In-month = built + opening - closing, the fulfilment numerator the whole
    project reports. NOT the seated or fed number -- mixing bases double-counts
    the month boundary (documented in PROBLEM_STATEMENT.md section 5).
    """
    inv = pl.read_parquet(rd / "l11_invariants.parquet")
    out: dict = {"pass": inv.filter(pl.col("status") == "PASS").height,
                 "n": inv.height}
    for plant in ("PCR", "TBR"):
        row = inv.filter(pl.col("invariant") == f"{plant} demand fulfilment")
        out[plant] = (float(str(row["actual"][0]).rstrip("%")) if row.height
                      else float("nan"))
    return out


def main() -> None:
    flags = dict(kv.split("=", 1) for kv in sys.argv[1:] if "=" in kv)
    if not flags:
        raise SystemExit(__doc__.split("\n\n")[1])

    label = " ".join(f"{k}={v}" for k, v in flags.items())
    print(f"\n  TWO-MONTH A/B GATE\n  candidate: {label}\n  {'-' * 66}")

    verdicts: list[float] = []
    for month in MONTHS:
        base = _score(_plan(month, f"ab_base_{month[-2:]}", {}))
        cand = _score(_plan(month, f"ab_cand_{month[-2:]}", flags))
        print(f"\n  {month}")
        for plant in ("PCR", "TBR"):
            d = cand[plant] - base[plant]
            mark = "OK " if d >= -TOL else "BAD"
            print(f"    {mark} {plant}  {base[plant]:6.1f}% -> {cand[plant]:6.1f}%"
                  f"   {d:+.1f} pt")
            verdicts.append(d)
        dp = cand["pass"] - base["pass"]
        print(f"        L11 {base['pass']}/{base['n']} -> {cand['pass']}/{cand['n']}"
              f"   {dp:+d}")
        verdicts.append(dp * TOL)      # an invariant is worth a tolerance unit

    worst, best = min(verdicts), max(verdicts)
    print(f"\n  {'-' * 66}")
    if worst < -TOL:
        print(f"  REJECT   worst month/plant is {worst:+.1f} pt.")
        print("           A flag that helps one month and hurts another is a fit "
              "to one\n           month's data, not an improvement. Do not ship.")
        raise SystemExit(1)
    if best <= TOL:
        print(f"  NEUTRAL  nothing moved beyond +/-{TOL} pt. Record it and move on.")
        raise SystemExit(2)
    print(f"  ADOPT    non-negative on both months, best {best:+.1f} pt.")
    print("           Record the per-month numbers in VERSION before shipping.")


if __name__ == "__main__":
    main()
