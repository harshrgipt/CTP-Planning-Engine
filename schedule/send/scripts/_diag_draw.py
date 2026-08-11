"""Read-only diagnostic: hourly cure DRAW implied by L5 vs BUILD capacity,
split by the group that actually owns the capacity (TT/TL on TBR, rim on PCR).

    python scripts/_diag_draw.py <run> <month>

The audit (EXPERT_AUDIT 4c) showed the PLANT-AGGREGATE draw never exceeds
plant-aggregate build capacity. This asks the same question inside each
capacity partition, and in each machine's OWN cadence.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
INP = paths.INPUT_DERIVED


def main() -> int:
    run = ROOT / "runs" / sys.argv[1]
    month = sys.argv[2]
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    H = ndays * 24

    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    cm = pl.read_parquet(D / f"cap_machine_{month}.parquet")
    grp = pl.read_parquet(D / f"cap_ttl_groups_{month}.parquet")
    tt = pl.read_parquet(INP / "tt_tl.parquet")
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{month}.parquet")
    import json
    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    cad = pl.read_parquet(PARAMS / P["tables"]["build_cadence"])
    cad_s = {r["machine"]: float(r["cadence_s_p50"]) for r in cad.iter_rows(named=True)}

    group_of = {r["machine"]: r["group"] for r in grp.iter_rows(named=True)}
    tmap = tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"]).unique(subset=["sku"])
    gt_tag = {r["gt_code"]: r["tt_tl"]
              for r in dem.join(tmap, on="sku", how="left").iter_rows(named=True)
              if r["tt_tl"] and r["plant"] == "TBR"}
    elig: dict[tuple, list] = {}
    for r in cm.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["machine"])

    def tag(p, gt):
        if p == "TBR" and gt_tag.get(gt):
            e = [x for x in elig.get((p, gt), []) if group_of.get(x) == gt_tag[gt]]
            if e:
                return gt_tag[gt]
        return "ALL"

    # build capacity per (plant, tag) in tyres/h -- sum of 1/cadence over machines
    machines = {}
    for (p, gt), ms in elig.items():
        for x in ms:
            machines[(p, x)] = group_of.get(x, "ALL")
    capacity = {}
    for (p, x), g in machines.items():
        c = cad_s.get(x)
        if c is None:
            continue
        for key in {(p, "ALL"), (p, g)}:
            capacity[key] = capacity.get(key, 0.0) + 3600.0 / c

    print("=" * 96)
    print(f"HOURLY CURE DRAW vs BUILD CAPACITY  --  {sys.argv[1]}  {month}")
    print("=" * 96)
    print("\n  BUILD CAPACITY (tyres/h, per-machine cadence, no setup deduction)")
    for k in sorted(capacity):
        n = sum(1 for (p, x), g in machines.items() if p == k[0] and (k[1] == "ALL" or g == k[1]))
        print(f"    {k[0]:<5} {k[1]:<5} {capacity[k]:8.1f} tyres/h   ({n} machines)")

    # hourly draw
    draw = {}
    for r in cc.iter_rows(named=True):
        p, gt = r["plant"], r["gt_code"]
        s, e = r["start_ts"], r["end_ts"]
        hours = (e - s).total_seconds() / 3600.0
        if hours <= 0:
            continue
        rate = r["qty"] / hours
        g = tag(p, gt)
        h0 = int((s - t0).total_seconds() // 3600)
        h1 = int((e - t0).total_seconds() // 3600)
        for h in range(max(0, h0), min(H, h1 + 1)):
            lo = max(s, t0 + timedelta(hours=h))
            hi = min(e, t0 + timedelta(hours=h + 1))
            if hi <= lo:
                continue
            f = (hi - lo).total_seconds() / 3600.0
            for key in {(p, "ALL"), (p, g)}:
                draw.setdefault((key, h), 0.0)
                draw[(key, h)] += rate * f

    print("\n  DRAW vs CAPACITY, by hour")
    for key in sorted(capacity):
        cap = capacity[key]
        series = [draw.get((key, h), 0.0) for h in range(H)]
        over = [h for h in range(H) if series[h] > cap]
        peak = max(series)
        print(f"\n    {key[0]} {key[1]}   cap {cap:6.1f}   peak draw {peak:6.1f}"
              f"  ({peak / cap * 100:5.1f}% of cap)   hours over cap {len(over)} of {H}")
        # daily mean draw
        line = []
        for d in range(ndays):
            dm = sum(series[d * 24:(d + 1) * 24]) / 24.0
            line.append(f"{d + 1:>2}:{dm / cap * 100:5.1f}%")
        for i in range(0, len(line), 8):
            print("       " + "  ".join(line[i:i + 8]))
        tot = sum(series)
        print(f"       total draw {tot:,.0f} tyre-h/h = {tot:,.0f} tyres;"
              f" flat rate if spread over {ndays}d = {tot / H:6.1f} tyres/h"
              f" ({tot / H / cap * 100:5.1f}% of cap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
