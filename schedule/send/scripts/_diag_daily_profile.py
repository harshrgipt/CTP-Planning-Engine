"""Daily press / machine occupancy profile for one run, recomputed from the plan.

    python scripts/_diag_daily_profile.py <run> [<run> ...] --month 2026-07

Reads ONLY cure_campaigns_reconciled.parquet and build_schedule.parquet.
Hours are CLIPPED into the day they are actually spent (DO-NOT: never bucket a
straddling row by its start day).  Denominator is the number of presses /
machines that appear anywhere in the plan x 24 h.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent


def _clip(rows, t0, nd, keycol, valcol=None):
    """Sum hours (or qty pro-rata) of each row into the days it spans."""
    prof = np.zeros(nd)
    qprof = np.zeros(nd)
    for r in rows:
        s, e = r["start_ts"], r["end_ts"]
        span = (e - s).total_seconds() / 3600.0
        if span <= 0:
            continue
        q = float(r.get(valcol, 0.0) or 0.0) if valcol else 0.0
        for d in range(nd):
            ds = t0 + timedelta(days=d)
            de = ds + timedelta(days=1)
            lo, hi = max(s, ds), min(e, de)
            if hi > lo:
                h = (hi - lo).total_seconds() / 3600.0
                prof[d] += h
                qprof[d] += q * h / span
    return prof, qprof


def profile(run: Path, month: str):
    y, m = int(month[:4]), int(month[5:7])
    import datetime as _dt
    t0 = _dt.datetime(y, m, 1, 7, 0)
    nd = 31 if m in (1, 3, 5, 7, 8, 10, 12) else 30
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    out = {}
    for p in ("PCR", "TBR"):
        rp = rec.filter(pl.col("plant") == p)
        bp = bs.filter((pl.col("plant") == p) &
                       (pl.col("machine") != "OPENING_STOCK"))
        npress = rp["press"].n_unique() or 1
        nmach = bp["machine"].n_unique() or 1
        ph, pq = _clip(rp.to_dicts(), t0, nd, "press", "qty")
        bh, bq = _clip(bp.to_dicts(), t0, nd, "machine", "qty")
        out[p] = dict(press_h=ph, press_q=pq, build_h=bh, build_q=bq,
                      npress=npress, nmach=nmach,
                      press_occ=100 * ph / (npress * 24),
                      build_occ=100 * bh / (nmach * 24))
    return out


def main() -> int:
    argv = sys.argv[1:]
    month = "2026-07"
    if "--month" in argv:
        i = argv.index("--month")
        month = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    res = [(n, profile(ROOT / "runs" / n, month)) for n in argv]
    for p in ("PCR", "TBR"):
        print("=" * (10 + 26 * len(res)))
        print(f"{p}   month={month}    press occ % | build occ % | cure tyres/day")
        print("=" * (10 + 26 * len(res)))
        print(f"{'day':<5}" + "".join(f"{n:>26}" for n, _ in res))
        nd = len(res[0][1][p]["press_h"])
        for d in range(nd):
            cells = ""
            for _, r in res:
                cells += (f"{r[p]['press_occ'][d]:>8.1f}"
                          f"{r[p]['build_occ'][d]:>8.1f}"
                          f"{r[p]['press_q'][d]:>10,.0f}")
            print(f"{d + 1:<5}{cells}")
        print(f"{'--':<5}" + "".join(f"{'-' * 26}" for _ in res))
        for lbl, key in (("mean occ", "press_occ"), ("mean bocc", "build_occ")):
            print(f"{lbl:<5}" + "".join(
                f"{np.mean(r[p][key]):>26.1f}" for _, r in res))
        # tail collapse metric: last 10 days mean press occ vs first 20
        for n, r in res:
            po = r[p]["press_occ"]
            print(f"   {n}: d1-20 {po[:20].mean():.1f}%  d21-31 "
                  f"{po[20:].mean():.1f}%  min {po.min():.1f}%  "
                  f"CV {po.std() / max(po.mean(), 1e-9):.3f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
