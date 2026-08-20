"""HONEST ARM SCORECARD -- BUILT, in-month, tail and total, for every arm.

    PYTHONPATH=. python scripts/arm_scorecard.py 2026-08 \
        base  "strict|PLANNER_HORIZON_MODE=strict"  "trunc|PLANNER_HORIZON_MODE=truncate"

WHY THIS EXISTS
  On 2026-08-13 a change was measured that moved August in-month fulfilment
  91.4 % -> 94.8 % and was very nearly shipped as the biggest win of the
  project. It was not a win: it BUILT 2,432 fewer tyres and total real output
  FELL. The whole apparent gain was the carry-out tail moving inside the
  horizon (23,381 -> 6,616) -- cures happening earlier, not more.

  IN-MONTH FULFILMENT IS TAIL-SENSITIVE. Any change that pulls cures earlier
  inflates it without producing anything. So no arm gets judged on in-month
  alone ever again. This script prints, per plant:

      BUILT           tyres produced on REAL MACHINES           <- the real one
                      (OPENING_STOCK pseudo-machine excluded)
      in-month        cured before the horizon ends           <- the KPI
      tail            cured after it (real output, next month)
      in-month+tail   total real output
      unfed           scheduled but never built

  A change that raises in-month while BUILT falls is RELOCATING output, not
  creating it. Say so in those words when reporting it.

THIS SCRIPT SEPARATES ARMS BY ENV ONLY -- IT CANNOT A/B A FILE
  Every arm in one invocation reads the SAME masters off disk. If the thing you
  are testing is a file rather than a flag -- `allowed_machine_matrix.parquet`,
  `gt_machine_partition.parquet`, a mined master -- then running "before" and
  "after" as two arms here measures nothing, and running them as two separate
  invocations with the file rewritten in between produces a table whose rows
  came from different inputs while looking like one experiment.

  That happened on 2026-08-17. A three-way July table (base 95.8 / partition
  96.4 / +weightage 96.6) was assembled across invocations with the matrix
  rewritten between them. Re-running the top arm against the state actually left
  on disk gives 96.0, and two identical arms in one invocation return the same
  number to the tyre -- so the engine is deterministic and the 0.6 pt was input
  drift, not a result. The August gate run the same day IS sound, because its
  driver script set the matrix explicitly before each arm.

  So: to A/B a file, drive it from a script that copies the file into place
  before each arm, and print the fingerprint. The banner below does the printing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import polars as pl

from planner.config import PRESS_ROSTER

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = ROOT / ".venv" / "bin" / "python"

MONTH_ENV = {
    # PLANNER_PARTITION_PLANTS="" was here because the GREEDY partition builder
    # needs the raw MES and so could not stamp a July file -- every July arm ran
    # unpartitioned as a result. `cpsat_partition.py` builds from committed
    # masters, so the override is gone and July now uses the engine default
    # (PCR only -- see l7_pull_release.py), the same as any other month.
    "2026-07": {"PLANNER_LOT_INTERVAL_H": "8",
                "PLANNER_TH_GT_WIP_RAIL_MARGIN": "1.0"},
    "2026-08": {"PLANNER_OPENING_GT": "opening_gt_manual_2026-08.parquet",
                "PLANNER_LOT_INTERVAL_H": "8",
                "PLANNER_TH_GT_WIP_RAIL_MARGIN": "1.0"},
}


def plan(month: str, run: str, extra: dict) -> Path | None:
    env = dict(os.environ)
    env.update(MONTH_ENV[month])
    env.update(extra)
    env["PYTHONPATH"] = "."
    env["PYTHONIOENCODING"] = "utf-8"
    out = Path(tempfile.mkdtemp(prefix=f"arm_{run}_"))
    r = subprocess.run([str(PY), "main.py", "plan", "--month", month, "--run", run,
                        "--out", str(out), "--btp-only"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  !! {run} FAILED")
        print((r.stdout or "")[-1200:])
        return None
    return ROOT / "runs" / run


def score(rd: Path, month: str) -> dict:
    inv = pl.read_parquet(rd / "l11_invariants.parquet")
    bs = pl.read_parquet(rd / "build_schedule.parquet")
    cc = pl.read_parquet(rd / "cure_campaigns.parquet")
    nr = pl.read_parquet(ROOT / "warehouse" / "derived"
                         / f"net_requirement_{month}.parquet").filter(
        ~pl.col("residual"))
    out = {"pass": inv.filter(pl.col("status") == "PASS").height, "n": inv.height}
    for p in ("PCR", "TBR"):
        row = inv.filter(pl.col("invariant") == f"{p} demand fulfilment")
        tr = inv.filter(pl.col("invariant").str.contains(f"{p} carry-out"))
        ful = float(str(row["actual"][0]).rstrip("%")) if row.height else float("nan")
        tail = float(str(tr["actual"][0]).replace(",", "")) if tr.height else 0.0
        d = nr.filter(pl.col("plant") == p)["demand"].sum()
        # EXCLUDE THE OPENING_STOCK PSEUDO-MACHINE. build_schedule carries a
        # row per GT for the floor stock the month opens with (Aug PCR 3,775,
        # Jul 4,352) and it is NOT production. Summing the whole frame overstated
        # BUILT by ~3.8k PCR / ~1.0k TBR in every figure quoted on 2026-08-13.
        # gt_events +d carries the same credit, so cross-checking one against the
        # other CANNOT catch it -- both include it.
        _b = bs.filter((pl.col("plant") == p)
                       & ~pl.col("machine").str.contains("OPENING"))
        built = _b["qty"].sum()
        sched = cc.filter(pl.col("plant") == p)["qty"].sum()
        # REAL press utilisation, not scheduled. A campaign L5 seated but
        # building never fed still carries its hours in cure_campaigns, so
        # summing that frame books an EMPTY press as busy -- it reported TBR at
        # 96.9 % when the real figure is 89.9 %. Deduct the unfed hours.
        # PLANT ROSTER, ruled 2026-08-14: 86 PCR / 79 TBR, every month.
        # Four masters (cycle_time_curing, allowed_press_matrix,
        # capacity_press_day, plant_profile) say 92/80 -- they are stale.
        npr = PRESS_ROSTER[p]                 # plant file, see config.py
        _c = cc.filter(pl.col("plant") == p)
        _rate = _c["qty"].sum() / max(_c["hours"].sum(), 1e-9)
        _unf_h = 0.0
        _rf = rd / "cure_campaigns_reconciled.parquet"
        if _rf.exists():
            _r = pl.read_parquet(_rf).filter(pl.col("plant") == p)
            if "qty_unfed" in _r.columns:
                _unf_h = float(_r["qty_unfed"].sum()) / max(_rate, 1e-9)
        # CLIP TO THE MONTH. `hours` is the whole campaign, and campaigns run
        # past the boundary under HORIZON_MODE=extend -- summing them unclipped
        # printed 102.0 % press utilisation on August PCR, which is impossible.
        _y, _m = int(month[:4]), int(month[5:7])
        _t0 = dt.datetime(_y, _m, 1, 7, 0)
        _end = dt.datetime(_y + (_m == 12), (_m % 12) + 1, 1, 7, 0)
        _hz = (_end - _t0).total_seconds() / 3600
        _cl = _c.with_columns(
            pl.min_horizontal(pl.col("end_ts"), pl.lit(_end)).alias("_e"),
            pl.max_horizontal(pl.col("start_ts"), pl.lit(_t0)).alias("_s"))
        _sched_h = (((_cl["_e"] - _cl["_s"]).dt.total_seconds() / 3600)
                    .clip(lower_bound=0).sum())
        _util = 100 * (_sched_h - _unf_h) / (npr * _hz)
        # IN-MONTH IS READ, NOT RECONSTRUCTED. It used to be `ful/100 * d`,
        # which multiplies L11's percentage by the WRONG DENOMINATOR: L11
        # computes fed/gross_build, and `d` here is `demand`. gross_build =
        # demand - opening cover + yield uplift, so the two differ ~1% AND THE
        # SIGN FLIPS BY PLANT -- opening cover dominates on PCR, yield uplift on
        # TBR. Measured 2026-08-14: Jul PCR +3,708 / TBR -538, Aug PCR +2,444 /
        # TBR -749. Deltas between arms were unaffected (both carry the ratio),
        # so no ship/reject decision moved -- but every absolute in-month figure
        # quoted from this script was wrong. Sixth instance of the basis class.
        _inm = 0.0
        _rf2 = rd / "cure_campaigns_reconciled.parquet"
        if _rf2.exists():
            _r2 = pl.read_parquet(_rf2).filter(pl.col("plant") == p)
            _fc = ("qty_fed_in_month" if "qty_fed_in_month" in _r2.columns
                   else "qty_fed")
            _inm = float(_r2[_fc].sum())
        # Two legitimate denominators, so print which one. `demand` is what the
        # customer asked for and is the fulfilment basis; `gross_build` is what
        # L7 must BUILD and is what L11's own percentage divides by.
        _gb = float(nr.filter(pl.col("plant") == p)["gross_build"].sum()) \
            if "gross_build" in nr.columns else d
        out[p] = dict(built=built, ful=100 * _inm / max(d, 1e-9), l11=ful,
                      inm=_inm, tail=tail, total=_inm + tail, demand=d,
                      gross_build=_gb, unfed=sched - built, press_util=_util)
    return out


def _file_state(month: str) -> list[str]:
    """Fingerprint the FILE-valued inputs every arm here necessarily shares.

    Printed so a table can be checked against the state that produced it. Two
    runs quoting different numbers under the same env differ HERE or nowhere --
    the engine itself is deterministic (verified 2026-08-17: two identical arms,
    identical to the tyre on both plants).
    """
    from planner import paths
    out = []
    for f in (paths.INPUT_DERIVED / "allowed_machine_matrix.parquet",
              paths.INPUT_DERIVED / "gt_machine_partition.parquet",
              ROOT / "warehouse" / "derived" / f"cap_machine_{month}.parquet"):
        if not f.exists():
            out.append(f"{f.name:<34} ABSENT")
            continue
        b = f.read_bytes()
        extra = ""
        if f.name == "gt_machine_partition.parquet":
            try:
                extra = f"  stamped {pl.read_parquet(f)['month'][0]}"
            except Exception:                                     # noqa: BLE001
                extra = "  stamp unreadable"
        out.append(f"{f.name:<34} sha1 {hashlib.sha1(b).hexdigest()[:12]}  "
                   f"{len(b):>7,} B{extra}")
    return out


def main() -> None:
    month = sys.argv[1]
    arms = []
    for spec in sys.argv[2:]:
        bits = spec.split("|")
        arms.append((bits[0], dict(kv.split("=", 1) for kv in bits[1:] if "=" in kv)))
    if not arms:
        raise SystemExit("give at least one arm, e.g.  base  \"strict|PLANNER_X=1\"")

    rows = []
    for name, env in arms:
        rd = plan(month, f"sc_{name}", env)
        if rd:
            rows.append((name, env, score(rd, month)))

    print(f"\n  ARM SCORECARD  {month}")
    print("  Judge on BUILT first. in-month alone is tail-sensitive and can rise "
          "while\n  production falls -- that is relocation, not improvement.")
    print("\n  SHARED FILE INPUTS (identical for every arm below -- a file-valued\n"
          "  difference CANNOT be A/B'd here; drive it from a script instead):")
    for line in _file_state(month):
        print(f"    {line}")
    print()
    for p in ("PCR", "TBR"):
        base = rows[0][2][p] if rows else None
        print(f"  {p}   (demand {base['demand']:,.0f})")
        print(f"    {'arm':<12}{'BUILT':>10}{'dBUILT':>9}{'in-month':>11}{'ful%':>7}"
              f"{'tail':>9}{'in+tail':>11}{'unfed':>8}{'press%':>8}  L11")
        for name, env, k in rows:
            v = k[p]
            db = v["built"] - base["built"]
            print(f"    {name:<12}{v['built']:>10,.0f}{db:>+9,.0f}{v['inm']:>11,.0f}"
                  f"{v['ful']:>7.1f}{v['tail']:>9,.0f}{v['total']:>11,.0f}"
                  f"{v['unfed']:>8,.0f}{v['press_util']:>7.1f}%  {k['pass']}/{k['n']}")
        print()
    for name, env, _ in rows:
        if env:
            print(f"    {name}: {' '.join(f'{a}={b}' for a, b in env.items())}")


if __name__ == "__main__":
    main()
