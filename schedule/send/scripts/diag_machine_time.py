"""DIAGNOSTIC (read-only): complete decomposition of building machine time.

    python scripts/diag_machine_time.py runs/aug_v3 2026-08

Every machine-hour in the horizon is attributed to exactly one bucket:
production, setup actually reserved, idle-gap-too-short-for-any-slice,
idle-gap-usable, leading (pre-first-run), trailing (post-last-run).
Nothing is left over -- the buckets sum to machines x days x 24.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import polars as pl
import calendar
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"


def main() -> None:
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs" / "aug_v3"
    if not run.is_absolute():
        run = ROOT / run
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-08"
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    days = calendar.monthrange(y, m)[1]
    t1 = t0 + timedelta(days=days)

    bs = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    size = pl.read_parquet(D / "gt_size.parquet")
    rim_of = {}
    for r in size.iter_rows(named=True):
        rim_of[r["gt_code"]] = r.get("rim")
    co = pl.read_parquet(D / "cap_changeover.parquet")
    same = {r["machine"]: float(r["same_min"]) for r in co.iter_rows(named=True)}
    diff = {r["machine"]: float(r["diff_min"]) for r in co.iter_rows(named=True)}
    cad = {r["machine"]: float(r["s_per_tyre"])
           for r in pl.read_parquet(D / "cycle_time_building.parquet").iter_rows(named=True)}

    def setup_h(mach, a, b):
        if a is None or a == b:
            return 0.0
        s = same.get(mach, 22.0)
        d = diff.get(mach, 42.0)
        return (s if rim_of.get(a, "@") == rim_of.get(b, "#") else d) / 60.0

    # machine roster: every machine that appears in the eligibility master for
    # the plant, not just the ones that got work (a stranded machine must show).
    am = pl.read_parquet(D / f"cap_machine_{month}.parquet")
    roster = {}
    for p in ("PCR", "TBR"):
        ms = sorted(set(am.filter(pl.col("plant") == p)["machine"].to_list())
                    | set(bs.filter(pl.col("plant") == p)["machine"].to_list()))
        roster[p] = ms

    for p in ("PCR", "TBR"):
        d = bs.filter(pl.col("plant") == p).sort(["machine", "start_ts"])
        if not d.height:
            continue
        H = days * 24.0
        machines = roster[p]
        prod = setupres = idle_short = idle_long = lead = trail = 0.0
        gaps = []
        per_mach = []
        # smallest atomic slice on this plant, in hours (what one more job needs)
        qs = bs.filter(pl.col("plant") == p)["qty"]
        cad_p = float(np.median([cad.get(mm, 62.0) for mm in machines]))
        slice_p50 = float(qs.median())
        slice_p10 = float(qs.quantile(0.10))
        atom_p50 = slice_p50 * cad_p / 3600.0
        atom_p10 = slice_p10 * cad_p / 3600.0
        for mach in machines:
            g = d.filter(pl.col("machine") == mach)
            mprod = msetup = mshort = mlong = 0.0
            if not g.height:
                lead += H
                per_mach.append((mach, 0.0, 0.0, 0.0, 0.0, H, 0.0, 0))
                continue
            rows = g.select(["gt_code", "start_ts", "end_ts"]).rows()
            # merge contiguous same-GT slices into runs
            runs = []
            cg, cs, ce = rows[0]
            for gt, s, e in rows[1:]:
                if gt == cg and abs((s - ce).total_seconds()) < 1.0:
                    ce = e
                else:
                    runs.append((cg, cs, ce))
                    cg, cs, ce = gt, s, e
            runs.append((cg, cs, ce))
            mprod = sum((e - s).total_seconds() / 3600.0 for _, s, e in runs)
            mlead = (runs[0][1] - t0).total_seconds() / 3600.0
            mtrail = (t1 - runs[-1][2]).total_seconds() / 3600.0
            for i in range(1, len(runs)):
                gap = (runs[i][1] - runs[i - 1][2]).total_seconds() / 3600.0
                need = setup_h(mach, runs[i - 1][0], runs[i][0])
                msetup += min(gap, need)
                free = max(0.0, gap - need)
                if free > 1e-9:
                    gaps.append(free)
                    if free < atom_p50:
                        mshort += free
                    else:
                        mlong += free
            prod += mprod
            setupres += msetup
            idle_short += mshort
            idle_long += mlong
            lead += mlead
            trail += mtrail
            per_mach.append((mach, mprod, msetup, mshort, mlong, mlead, mtrail,
                             len(runs)))
        tot = len(machines) * H
        print("=" * 100)
        print(f"{p}  {month}  run={run.name}   {len(machines)} machines x {H:.0f} h = {tot:,.0f} machine-h")
        print(f"   slice qty p10/p50 = {slice_p10:.0f}/{slice_p50:.0f}  "
              f"cadence p50 {cad_p:.0f}s  -> one slice = {atom_p10:.2f}/{atom_p50:.2f} h")
        rows = [("production (tyres on machine)", prod),
                ("setup RESERVED between runs", setupres),
                ("idle: gap < one median slice", idle_short),
                ("idle: gap >= one median slice", idle_long),
                ("idle: before first run (lead)", lead),
                ("idle: after last run (trail)", trail)]
        for k, v in rows:
            print(f"   {k:<34}{v:>10,.0f} h  {100*v/tot:>6.2f}%")
        print(f"   {'TOTAL':<34}{sum(v for _, v in rows):>10,.0f} h  "
              f"{100*sum(v for _, v in rows)/tot:>6.2f}%")
        if gaps:
            a = np.array(gaps)
            print(f"   inter-run gaps: n={len(a)}  sum={a.sum():,.0f} h  "
                  f"p10={np.percentile(a,10):.2f} p50={np.percentile(a,50):.2f} "
                  f"p90={np.percentile(a,90):.2f} max={a.max():.1f}")
            for thr, lbl in [(atom_p10, "p10-slice"), (atom_p50, "p50-slice")]:
                usable = a[a >= thr]
                print(f"     gaps >= {lbl} ({thr:.2f} h): n={len(usable)} "
                      f"sum={usable.sum():,.0f} h "
                      f"({100*usable.sum()/max(a.sum(),1):.0f}% of gap hours) "
                      f"-> {usable.sum()*3600/cad_p:,.0f} tyres of headroom")
        print(f"   {'machine':<16}{'prod':>9}{'setup':>8}{'short':>8}{'long':>8}"
              f"{'lead':>8}{'trail':>8}{'runs':>7}{'occ%':>7}")
        for mm, a1, a2, a3, a4, a5, a6, nr in sorted(per_mach, key=lambda x: -x[1]):
            print(f"   {mm:<16}{a1:>9.0f}{a2:>8.0f}{a3:>8.0f}{a4:>8.0f}"
                  f"{a5:>8.0f}{a6:>8.0f}{nr:>7}{100*(a1+a2)/H:>7.1f}")


if __name__ == "__main__":
    main()
