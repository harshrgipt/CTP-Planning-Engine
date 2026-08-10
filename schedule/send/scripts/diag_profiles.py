"""DIAGNOSTIC (read-only): daily build / cure / GT-stock profile of a plan.

    python scripts/diag_profiles.py runs/aug_v3 2026-08

Everything is TIME-based: a run's hours and tyres are clipped into the plant-day
(07:00 -> 07:00) they are actually spent in, never bucketed by start day.
GT stock is the hourly step function of build-completions minus cure-draws,
averaged over each plant-day (the same basis as the G8 rail).
"""
from __future__ import annotations
import sys, calendar
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent


def spread(rows, t0, ndays, qcol="qty"):
    """Spread each (start, end, qty) uniformly over the plant-days it covers."""
    out = np.zeros(ndays)
    for s, e, q in rows:
        if e <= s:
            e = s + timedelta(seconds=1)
        dur = (e - s).total_seconds()
        for d in range(ndays):
            a = t0 + timedelta(days=d)
            b = a + timedelta(days=1)
            ov = (min(e, b) - max(s, a)).total_seconds()
            if ov > 0:
                out[d] += q * ov / dur
    return out


def main() -> None:
    run = Path(sys.argv[1])
    run = run if run.is_absolute() else ROOT / run
    month = sys.argv[2]
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    nd = calendar.monthrange(y, m)[1]

    bs = pl.read_parquet(run / "build_schedule.parquet")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")

    for p in ("PCR", "TBR"):
        b = bs.filter((pl.col("plant") == p) & (pl.col("machine") != "OPENING_STOCK"))
        c = rec.filter(pl.col("plant") == p)
        build = spread(b.select(["start_ts", "end_ts", "qty"]).rows(), t0, nd)
        mh = spread([(s, e, (e - s).total_seconds() / 3600.0)
                     for s, e, _ in b.select(["start_ts", "end_ts", "qty"]).rows()], t0, nd)
        cure = spread([(s, e, q) for s, e, q in
                       c.select(["start_ts", "end_ts", "qty_fed"]).rows()], t0, nd)
        # hourly GT stock: +qty at build end, -qty at cure_ts, from the slices
        HH = nd * 24 + 200
        g = np.zeros(HH + 2)

        def hr(ts):
            return max(0, min(HH, int((ts - t0).total_seconds() // 3600)))
        for _m, s, e, q, ct in bs.filter(pl.col("plant") == p).select(
                ["machine", "start_ts", "end_ts", "qty", "cure_ts"]).rows():
            g[hr(e)] += q
            g[hr(ct)] -= q
        lvl = np.cumsum(g)[: nd * 24]
        dmean = lvl.reshape(-1, 24).mean(axis=1)
        print("=" * 96)
        print(f"{p} {month} {run.name}   lambda={build.sum()/ (nd*24):.0f} tyres/h  "
              f"time-wt mean stock={lvl.mean():,.0f}  daily-mean max={dmean.max():,.0f}")
        print(f"   {'day':>4}{'built':>9}{'cured':>9}{'mach-h':>9}{'GTstock':>9}")
        for d in range(nd):
            print(f"   {d+1:>4}{build[d]:>9,.0f}{cure[d]:>9,.0f}{mh[d]:>9.0f}{dmean[d]:>9,.0f}")
        interior = build[1:nd - 1]
        print(f"   interior(d2..d{nd-1}) mean built {interior.mean():,.0f}  CV {interior.std()/interior.mean():.3f}")
        print(f"   day1 {build[0]:,.0f} ({100*build[0]/interior.mean():.0f}% of interior)  "
              f"day{nd-1} {build[nd-2]:,.0f} ({100*build[nd-2]/interior.mean():.0f}%)  "
              f"day{nd} {build[nd-1]:,.0f} ({100*build[nd-1]/interior.mean():.0f}%)")
        print(f"   last 3 days built {build[-3:].sum():,.0f} vs 3x interior {3*interior.mean():,.0f} "
              f"= {100*build[-3:].sum()/(3*interior.mean()):.0f}%")
        print(f"   machine-h: interior mean {mh[1:nd-1].mean():.0f}/day, last3 "
              f"{mh[-3:].mean():.0f}/day = {100*mh[-3:].mean()/mh[1:nd-1].mean():.0f}%")


if __name__ == "__main__":
    main()
