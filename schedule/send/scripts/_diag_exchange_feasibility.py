"""TASK-2 FEASIBILITY (diagnostic only -- changes no plan byte).

Does a LEGAL EXCHANGE even exist for a starved campaign?

DO-NOT #30: verify a proposed gate/repair is BINDING -- here, REACHABLE -- before
building the exact version of it. A parallel agent already proved gap-search
finds nothing (0 of 57 crossing Aug PCR campaigns fit any eligible contiguous
last-week window). The exchange premise is that the useful contiguous capacity is
OCCUPIED by another flexible campaign, so only a swap can reach it.

This measures the UPPER BOUND on that premise. For each starved campaign S:

  * eligible machines resolved as L7's `_locked` does (partition -> home -> rim
    lock), never `cap_machine` (~3x wider, not what binds)
  * for every candidate later cure time T (taken from the OTHER campaigns
    actually in the plan), the R5 band is [T - 72 h, T - tau_min]
  * inside that band, on each eligible machine, we allow EVICTING ANY ONE other
    build run -- the most generous exchange conceivable
  * ask: does a CONTIGUOUS window >= S's own build duration then exist?

If the answer is no even under one-free-eviction, no exchange rule can recover
that volume and TASK 2 is dead before it is written.

    PYTHONPATH=. python scripts/_diag_exchange_feasibility.py SHIP2_aug 2026-08
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from planner import paths                       # noqa: E402
from planner.cmbc import plant_ct               # noqa: E402
from planner.config import GT_SHELF_LIFE_H      # noqa: E402


def main() -> int:
    run = sys.argv[1] if len(sys.argv) > 1 else "SHIP2_aug"
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-08"
    plant = sys.argv[3] if len(sys.argv) > 3 else "PCR"
    rd = ROOT / "runs" / run
    ct = plant_ct.get()

    P0 = json.loads(sorted((ROOT / "warehouse" / "params").glob("params_*.json"))[-1]
                    .read_text())
    tau_min_h = float(P0["tau"][plant]["tau_min_h"])

    import datetime as _dtm
    _y, _m = int(month[:4]), int(month[5:7])
    T0 = _dtm.datetime(_y, _m, 1, 7, 0)
    st = pl.read_parquet(rd / "build_starved.parquet").filter(pl.col("plant") == plant)
    bs = pl.read_parquet(rd / "build_schedule.parquet").filter(
        (pl.col("plant") == plant) & ~pl.col("machine").str.contains("OPENING"))
    cc = pl.read_parquet(rd / "cure_campaigns.parquet").filter(pl.col("plant") == plant)

    sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet") \
        .filter(pl.col("plant") == plant)
    rim_of = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)}
    part_of: dict[str, list] = defaultdict(list)
    for r in pl.read_parquet(paths.INPUT_DERIVED / "gt_machine_partition.parquet") \
            .filter(pl.col("plant") == plant).iter_rows(named=True):
        part_of[r["gt_code"]].append(r["machine"])
    home_of: dict[str, list] = defaultdict(list)
    hf = paths.INPUT_DERIVED / "gt_home_machine.parquet"
    if hf.exists():
        for r in (pl.read_parquet(hf).filter(pl.col("plant") == plant)
                  .sort(["gt_code", "rank"]).iter_rows(named=True)):
            home_of[r["gt_code"]].append(r["machine"])
    lock_of: dict[str, list] = defaultdict(list)
    lf = paths.INPUT_DERIVED / "machine_rim_lock.parquet"
    if lf.exists():
        _tr = {"hard": 0, "primary": 1, "flex": 2}
        tmp: dict[str, list] = defaultdict(list)
        for r in pl.read_parquet(lf).filter(pl.col("plant") == plant).iter_rows(named=True):
            tmp[str(r["locked_rim"])].append(
                (_tr.get(str(r["tier"]).lower(), 3), int(r["rank"]), r["machine"]))
        for k, v in tmp.items():
            lock_of[k] = [m for _a, _b, m in sorted(v)]

    def elig(gt: str) -> list:
        rim = rim_of.get(gt, "")
        if part_of.get(gt):
            pm = part_of[gt]
            return pm + [m for m in lock_of.get(rim, []) if m not in pm]
        hm = home_of.get(gt, [])
        return hm + [m for m in lock_of.get(rim, []) if m not in hm]

    # realised build runs per machine, sorted
    runs_on: dict[str, list] = defaultdict(list)
    for r in bs.iter_rows(named=True):
        runs_on[r["machine"]].append((r["start_ts"], r["end_ts"], r["gt_code"]))
    for m in runs_on:
        runs_on[m].sort()

    cure_times = sorted({r["start_ts"] for r in cc.iter_rows(named=True)})

    def best_gap(m: str, lo, hi, evict: bool) -> float:
        """Largest contiguous free hours in [lo,hi] on m, optionally allowing
        ONE occupying run to be evicted (the most generous exchange there is)."""
        occ = [(max(s, lo), min(e, hi)) for s, e, _g in runs_on.get(m, [])
               if e > lo and s < hi]
        occ = [(s, e) for s, e in occ if e > s]
        if not occ:
            return (hi - lo).total_seconds() / 3600.0
        best = 0.0
        # no eviction
        cur = lo
        for s, e in occ:
            best = max(best, (s - cur).total_seconds() / 3600.0)
            cur = max(cur, e)
        best = max(best, (hi - cur).total_seconds() / 3600.0)
        if not evict:
            return best
        # evict each single run in turn, recompute
        for i in range(len(occ)):
            rem = occ[:i] + occ[i + 1:]
            cur = lo
            b = 0.0
            for s, e in rem:
                b = max(b, (s - cur).total_seconds() / 3600.0)
                cur = max(cur, e)
            b = max(b, (hi - cur).total_seconds() / 3600.0)
            best = max(best, b)
        return best

    rows = []
    for r in st.iter_rows(named=True):
        gt, q, tc = r["gt_code"], float(r["qty"]), r["t_cure"]
        ms = elig(gt)
        if not ms or q <= 0:
            continue
        ct_s = ct.build_ct_s(plant, gt, ms[0]) or 0.0
        if ct_s <= 0:
            continue
        need_h = q * ct_s / 3600.0
        # (a) where it is now, no eviction  (b) now, evicting one
        # (c) ANY later candidate slot, evicting one  <- the exchange upper bound
        import datetime as _dt
        def band(T):
            # CLAMP TO t0. The R5 band routinely starts BEFORE the horizon, and
            # unclamped that pre-t0 time reads as free capacity -- which is the
            # `release_before_t0` population (6,810 tyres) counted as though it
            # were reachable. A month total of free hours is not availability
            # (DO-NOT #44); neither is an hour the plan may not use at all.
            return (max(T - _dt.timedelta(hours=GT_SHELF_LIFE_H), T0),
                    T - _dt.timedelta(hours=tau_min_h))
        lo0, hi0 = band(tc)
        now_ne = max((best_gap(m, lo0, hi0, False) for m in ms), default=0.0)
        now_ev = max((best_gap(m, lo0, hi0, True) for m in ms), default=0.0)
        later = [T for T in cure_times if T > tc]
        best_later = 0.0
        for T in later:
            lo, hi = band(T)
            g = max((best_gap(m, lo, hi, True) for m in ms), default=0.0)
            if g > best_later:
                best_later = g
                if best_later >= need_h:
                    break
        rows.append(dict(gt=gt, qty=q, need_h=need_h, n_mach=len(ms),
                         now_ne=now_ne, now_ev=now_ev, later_ev=best_later))

    df = pl.DataFrame(rows)
    tot = df["qty"].sum()
    print(f"== TASK-2 EXCHANGE FEASIBILITY  {run}  {plant} ==")
    print(f"   starved rows {df.height}   tyres {tot:,.0f}"
          f"   tau_min {tau_min_h:.2f} h   R5 band {GT_SHELF_LIFE_H:.0f} h")
    print()
    # SETUP IS NOT FREE. A run needs its changeover RESERVED before it, and the
    # plant charges PCR 22-28 min same-size / 42-60 min different-size. A gap
    # equal to the run length is NOT a placeable gap. Reporting the sweep rather
    # than one number, because the answer is entirely driven by this assumption
    # (and DO-NOT #20: never write a measured-looking table from reasoning).
    print(f"   {'setup allowance':<20}{'fits NOW (gap-search)':>26}"
          f"{'fits NOW +evict':>20}{'fits LATER +evict':>20}")
    for su_min in (0, 22, 28, 42, 60):
        su = su_min / 60.0
        a = df.filter(pl.col("now_ne") >= pl.col("need_h") + su)
        b = df.filter(pl.col("now_ev") >= pl.col("need_h") + su)
        c = df.filter(pl.col("later_ev") >= pl.col("need_h") + su)
        print(f"   {str(su_min) + ' min':<20}"
              f"{str(a.height) + '/' + str(df.height) + '  ' + format(a['qty'].sum(), ',.0f'):>26}"
              f"{str(b.height) + '  ' + format(b['qty'].sum(), ',.0f'):>20}"
              f"{str(c.height) + '  ' + format(c['qty'].sum(), ',.0f'):>20}")
    print()
    print("   need_h vs best contiguous hours available (p50 / max):")
    for c in ("need_h", "now_ne", "now_ev", "later_ev"):
        print(f"     {c:<10} p50 {df[c].median():>7.2f}   max {df[c].max():>7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
