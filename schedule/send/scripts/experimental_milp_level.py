"""Experimental MILP upper bound for PCR daily build levelling.

Keeps every physical run on its assigned machine and inside a conservative
R5/deadline day window.  It deliberately ignores within-day sequencing/setup,
so its result is an upper bound to be validated by L7, never a production plan.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--target", type=float, default=12000.0)
    a = ap.parse_args()
    rd = Path(__file__).resolve().parent.parent / "runs" / a.run
    b = pl.read_parquet(rd / "build_by_shift.parquet").filter(
        pl.col("plant") == "PCR")
    # Shift-clipped pieces are <=8 h and therefore assignable to a machine-day.
    # Moving them independently relaxes setup contiguity, another reason this is
    # an upper bound rather than an executable schedule.
    runs = b.select("machine", "qty", "start_ts", "end_ts",
                    pl.col("cure_ts").alias("first_cure"))
    t0 = b["start_ts"].min().replace(hour=7, minute=0, second=0,
                                      microsecond=0)
    nd = 31
    rec = []
    for i, r in enumerate(runs.iter_rows(named=True)):
        dur = max((r["end_ts"] - r["start_ts"]).total_seconds()/3600, .01)
        earliest = r["first_cure"] - timedelta(hours=66 + dur)
        latest = r["first_cure"] - timedelta(hours=.27 + dur)
        lo = max(1, int((earliest-t0).total_seconds()//86400)+1)
        hi = min(nd, int((latest-t0).total_seconds()//86400)+1)
        old = min(nd, max(1, int((r["start_ts"]-t0).total_seconds()//86400)+1))
        for d in range(lo, hi+1):
            rec.append((i, d, r["machine"], float(r["qty"]), dur, old))
    nedge, nr = len(rec), runs.height
    nvar = nedge + nd
    c = np.zeros(nvar)
    for j, (_, d, _, _, dur, old) in enumerate(rec):
        c[j] = abs(d-old)*0.05 + max(old-d, 0)*dur*0.002
    c[nedge:] = 1000.0
    constraints = []
    # Each run assigned once.
    A = lil_matrix((nr, nvar));
    for j, (i, *_rest) in enumerate(rec): A[i, j] = 1
    constraints.append(LinearConstraint(A.tocsr(), np.ones(nr), np.ones(nr)))
    # Each machine/day has at most 24 productive hours.
    keys = sorted({(m,d) for _,d,m,*_ in rec}); km={k:i for i,k in enumerate(keys)}
    A = lil_matrix((len(keys), nvar))
    for j, (_,d,m,_q,h,_old) in enumerate(rec): A[km[(m,d)],j]=h
    constraints.append(LinearConstraint(A.tocsr(), -np.inf, np.full(len(keys),24.0)))
    # Daily qty + shortfall >= target.
    A = lil_matrix((nd,nvar))
    for j, (_i,d,_m,q,_h,_old) in enumerate(rec): A[d-1,j]=q
    for d in range(nd): A[d,nedge+d]=1
    constraints.append(LinearConstraint(A.tocsr(), np.full(nd,a.target), np.inf))
    res=milp(c,integrality=np.r_[np.ones(nedge),np.zeros(nd)],
             bounds=Bounds(np.zeros(nvar),np.r_[np.ones(nedge),np.full(nd,np.inf)]),
             constraints=constraints,options={"time_limit":120})
    print("status",res.message)
    if res.x is None: return
    daily=np.zeros(nd); moved=0
    for j,(_i,d,_m,q,_h,old) in enumerate(rec):
        if res.x[j]>.5:
            daily[d-1]+=q; moved += d != old
    print("runs",nr,"edges",nedge,"moved",moved,"total",round(daily.sum()))
    print("days<target",int((daily<a.target-.5).sum()),"shortfall",round(np.maximum(a.target-daily,0).sum()))
    for d,q in enumerate(daily,1): print(f"day {d:02d} {q:,.0f}")

if __name__ == "__main__": main()
