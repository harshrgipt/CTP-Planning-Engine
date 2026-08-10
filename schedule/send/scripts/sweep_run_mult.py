"""Sweep the L7 run target and print the trade curve.

    PYTHONPATH=. python scripts/sweep_run_mult.py 2026-07 1.0 1.5 2.0 3.0

Every arm is run FRESH into its own directory. Never baseline a new arm against
an existing run directory: RunContext hashes the config, but PLANNER_* env flags
read through os.environ are not in that hash, so two arms can be
indistinguishable on disk.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
FLOOR = {"PCR": 150, "TBR": 70}
BAND = {"PCR": (6.0, 10.0), "TBR": (4.0, 7.0)}


def measure(run: Path) -> dict:
    b = pl.read_parquet(run / "build_schedule.parquet")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    fresh = b.filter(pl.col("machine") != "OPENING_STOCK")
    runs = (fresh.group_by(["plant", "machine", "run_id"])
            .agg(pl.col("qty").sum().alias("q"),
                 pl.col("start_ts").min().alias("t0"),
                 pl.col("end_ts").max().alias("t1"))
            .with_columns(((pl.col("t1") - pl.col("t0")).dt.total_seconds()
                           / 3600.0).alias("h")))
    out: dict = {}
    for p in ("PCR", "TBR"):
        rp = runs.filter(pl.col("plant") == p)
        bp = b.filter(pl.col("plant") == p)
        fp = fresh.filter(pl.col("plant") == p)
        if not rp.height:
            continue
        lo, hi = BAND[p]
        mdays = (fp.with_columns(pl.col("start_ts").dt.date().alias("d"))
                 .select(["machine", "d"]).unique().height)
        w = np.array(bp["wait_h"], float)
        ev = pl.concat([
            bp.select([pl.col("end_ts").alias("ts"), pl.col("qty").alias("d")]),
            bp.select([pl.col("cure_ts").alias("ts"), (-pl.col("qty")).alias("d")]),
        ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
        r2 = rec.filter(pl.col("plant") == p)
        out[p] = {
            "runs": rp.height,
            "qty_p50": float(rp["q"].median()),
            "below": 100.0 * rp.filter(pl.col("q") < FLOOR[p]).height / rp.height,
            "in_band": 100.0 * rp.filter((pl.col("h") >= lo)
                                         & (pl.col("h") <= hi)).height / rp.height,
            "chg": (rp.height - fp["machine"].n_unique()) / max(mdays, 1),
            "head_p50": float(np.percentile(w, 50)),
            "head_max": float(w.max()),
            "r5": int((w > 72.0).sum()),
            "inv": float(ev["bal"].mean()),
            "fed": 100.0 * float(r2["qty_fed"].sum()) / max(float(r2["qty"].sum()), 1),
        }
    return out


def main() -> None:
    month = sys.argv[1]
    mults = [float(x) for x in sys.argv[2:]] or [1.0]
    # Must be a cure plan built by the CURRENT L4/L5. Seeding from an older run
    # would silently mix a pre-fix cure requirement into every arm.
    src = ROOT / "runs" / "july_cmbc_v5" / "cure_campaigns.parquet"
    rows = []
    for k in mults:
        run = ROOT / "runs" / f"sweep_T{k:g}"
        shutil.rmtree(run, ignore_errors=True)
        run.mkdir(parents=True)
        shutil.copy(src, run / "cure_campaigns.parquet")
        env = {**os.environ, "PLANNER_LOT_INTERVAL_H": str(k),
               "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run(
            [str(ROOT / ".venv" / "Scripts" / "python.exe"),
             "-m", "planner.cmbc.l7_pull_release", "--month", month,
             "--run", run.name],
            env=env, cwd=ROOT, capture_output=True, text=True)
        if not (run / "build_schedule.parquet").exists():
            print(f"k={k}: FAILED\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
            continue
        rows.append((k, measure(run)))

    hdr = (f"{'k':>5}{'plant':>6}{'runs':>7}{'qty p50':>9}{'<floor':>8}"
           f"{'in band':>9}{'chg/md':>8}{'head p50':>10}{'head max':>10}"
           f"{'R5>72':>7}{'inv':>9}{'% fed':>8}")
    print(hdr)
    print("-" * len(hdr))
    for k, m in rows:
        for p in ("PCR", "TBR"):
            if p not in m:
                continue
            v = m[p]
            print(f"{k:>5g}{p:>6}{v['runs']:>7,}{v['qty_p50']:>9,.0f}"
                  f"{v['below']:>7.1f}%{v['in_band']:>8.1f}%{v['chg']:>8.2f}"
                  f"{v['head_p50']:>10.2f}{v['head_max']:>10.2f}{v['r5']:>7}"
                  f"{v['inv']:>9,.0f}{v['fed']:>7.1f}%")


if __name__ == "__main__":
    main()
