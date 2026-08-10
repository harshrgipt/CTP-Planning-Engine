"""Read-only diagnostic: daily press occupancy + starvation-by-day profile.

    python scripts/_diag_profile.py <run> <month>

Recomputes from cure_campaigns_reconciled.parquet / build_schedule.parquet.
Hours are CLIPPED into the day they are actually spent (MEMORY §12).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"


def main() -> int:
    run = ROOT / "runs" / sys.argv[1]
    month = sys.argv[2]
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    horizon = t0 + timedelta(days=ndays)

    cc = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    press = pl.read_parquet(D / f"cap_press_{month}.parquet")
    n_press = {p: press.filter(pl.col("plant") == p)["press"].n_unique()
               for p in ("PCR", "TBR")}

    print("=" * 96)
    print(f"DAILY PRESS OCCUPANCY  --  {sys.argv[1]}  {month}")
    print(f"  presses: PCR {n_press['PCR']}  TBR {n_press['TBR']}")
    print("=" * 96)

    # clip each campaign's hours into each plant-day it touches
    rows = []
    for r in cc.iter_rows(named=True):
        s, e = r["start_ts"], r["end_ts"]
        if e is None or s is None:
            continue
        s = max(s, t0)
        e = min(e, horizon)
        if e <= s:
            continue
        d = int((s - t0).total_seconds() // 86400)
        while True:
            ds = t0 + timedelta(days=d)
            de = ds + timedelta(days=1)
            lo, hi = max(s, ds), min(e, de)
            if hi > lo:
                rows.append({"plant": r["plant"], "day": d + 1,
                             "h": (hi - lo).total_seconds() / 3600.0})
            if de >= e:
                break
            d += 1
    occ = (pl.DataFrame(rows).group_by(["plant", "day"])
           .agg(pl.col("h").sum()).sort(["plant", "day"]))

    for p in ("PCR", "TBR"):
        s = occ.filter(pl.col("plant") == p)
        cap = n_press[p] * 24.0
        print(f"\n  {p}   (capacity {cap:.0f} press-h/day)")
        line = []
        for d in range(1, ndays + 1):
            v = s.filter(pl.col("day") == d)["h"]
            u = (float(v[0]) / cap * 100.0) if len(v) else 0.0
            line.append(f"{d:>2}:{u:5.1f}%")
        for i in range(0, len(line), 8):
            print("     " + "  ".join(line[i:i + 8]))
        # last-10 vs first-20
        tot = {d: (float(s.filter(pl.col("day") == d)["h"][0])
                   if len(s.filter(pl.col("day") == d)) else 0.0)
               for d in range(1, ndays + 1)}
        f20 = sum(tot[d] for d in range(1, 21)) / (20 * cap) * 100
        l10 = sum(tot[d] for d in range(ndays - 9, ndays + 1)) / (10 * cap) * 100
        idle_tail = sum(cap - tot[d] for d in range(ndays - 9, ndays + 1))
        print(f"     days 1-20 mean {f20:5.1f}%   last 10 days mean {l10:5.1f}%"
              f"   idle press-h in last 10 days {idle_tail:,.0f}")

    # ---- starvation by day: which cure campaigns went unfed, and when -------
    print("\n" + "=" * 96)
    print("STARVATION BY CAMPAIGN START DAY  (qty_unfed from the reconciled plan)")
    print("=" * 96)
    cc2 = cc.with_columns(
        ((pl.col("start_ts") - pl.lit(t0)).dt.total_seconds() // 86400 + 1)
        .cast(pl.Int64).alias("day"))
    for p in ("PCR", "TBR"):
        s = (cc2.filter(pl.col("plant") == p)
             .group_by("day").agg(pl.col("qty_unfed").sum().alias("unfed"),
                                  pl.col("qty").sum().alias("q"))
             .sort("day"))
        tot = float(cc2.filter(pl.col("plant") == p)["qty_unfed"].sum())
        print(f"\n  {p}  total unfed {tot:,.0f}")
        cum = 0.0
        for r in s.iter_rows(named=True):
            cum += r["unfed"]
            if r["unfed"] > 0:
                print(f"     day {r['day']:>2}  unfed {r['unfed']:>8,.0f}"
                      f"  of {r['q']:>8,.0f}  cum {cum / max(tot, 1) * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
