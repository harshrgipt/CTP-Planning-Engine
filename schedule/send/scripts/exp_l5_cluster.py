"""EXPERIMENT: does clustering a GT's cure campaigns in time deliver the plant's
lot size and GT inventory?  Changes NO production code.

    PYTHONPATH=. python scripts/exp_l5_cluster.py 2026-07

WHAT IS BEING TESTED
  L5 sorts jobs globally by size (biggest first) and gives each the eligible
  press that frees EARLIEST. A GT's 3 campaigns therefore sit at 3 widely
  separated queue positions and land at 3 widely separated times -- sequential,
  not concurrent. Measured consequence: a GT is live 592 h of 744, using p50 2
  of its 4 available moulds, and only 3 of 40 PCR GTs ever reach their cap.

  The variant groups a GT's lots together in the queue (biggest GT first, so the
  scarcity priority survives), which makes them take presses that are free at
  about the same moment -- i.e. concurrently, bounded by the mould cap that L5
  already enforces.

  Campaign COUNT and SIZE are untouched, so total press-hours are unchanged.
  This re-arranges tiles; it does not re-cut them.

HOW
  L5's source is read and ONE line -- the job sort -- is replaced in memory,
  then executed. The variant cannot drift from the original: everything else is
  literally the same code. Downstream layers run unmodified via subprocess.
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
L5 = ROOT / "planner" / "cmbc" / "l5_cure_master.py"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

OLD = 'jobs.sort(key=lambda j: (j["plant"], -j["qty"], j["gt_code"], j["seq"]))'
NEW = (
    '_gtot = {}\n'
    '    for _j in jobs:\n'
    '        _k = (_j["plant"], _j["gt_code"])\n'
    '        _gtot[_k] = _gtot.get(_k, 0.0) + _j["qty"]\n'
    '    jobs.sort(key=lambda j: (j["plant"], -_gtot[(j["plant"], j["gt_code"])],\n'
    '                            j["gt_code"], -j["qty"], j["seq"]))'
)

FLOOR = {"PCR": 150, "TBR": 70}
PLANT = {  # July 2026 MES, same run definition
    "PCR": dict(runs=753, p50=363, p05=77, p95=1264, hrs=7.31, chg=2.18,
                below=12.7, inv=4772),
    "TBR": dict(runs=898, p50=86, p05=27, p95=219, hrs=5.64, chg=3.19,
                below=30.8, inv=1743),
}


def run_l5(month: str, out: str, cluster: bool) -> None:
    src = L5.read_text(encoding="utf-8")
    if cluster:
        if OLD not in src:
            raise SystemExit("L5 sort line not found -- L5 changed, update OLD")
        src = src.replace(OLD, NEW)
    g = {"__name__": "__main__", "__file__": str(L5)}
    argv = sys.argv
    sys.argv = ["l5_cure_master", "--month", month, "--out", out]
    try:
        exec(compile(src, str(L5), "exec"), g)          # noqa: S102
    finally:
        sys.argv = argv


def sh(mod: str, month: str, run: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([PY, "-m", f"planner.cmbc.{mod}", "--month", month,
                        "--run", run], env=env, cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout + r.stderr


def measure(run: Path) -> dict:
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    b = pl.read_parquet(run / "build_schedule.parquet")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    fresh = b.filter(pl.col("machine") != "OPENING_STOCK")
    out = {}
    for p in ("PCR", "TBR"):
        c = camp.filter(pl.col("plant") == p)
        bp = b.filter(pl.col("plant") == p)
        fp = fresh.filter(pl.col("plant") == p)
        if not c.height or not fp.height:
            continue
        # n_active: union live span per GT, summed
        live = 0.0
        for (_gt,), g in c.group_by("gt_code"):
            iv = sorted(zip(g["start_ts"].to_list(), g["end_ts"].to_list()))
            merged = []
            for s, e in iv:
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            live += sum((e - s).total_seconds() / 3600 for s, e in merged)
        runs = (fp.group_by(["machine", "run_id"])
                .agg(pl.col("qty").sum().alias("q"),
                     pl.col("start_ts").min().alias("t0"),
                     pl.col("end_ts").max().alias("t1"))
                .with_columns(((pl.col("t1") - pl.col("t0")).dt.total_seconds()
                               / 3600.0).alias("h")))
        q = np.array(runs["q"], float)
        w = np.array(bp["wait_h"], float)
        md = (fp.with_columns(pl.col("start_ts").dt.date().alias("d"))
              .select(["machine", "d"]).unique().height)
        ev = pl.concat([
            bp.select([pl.col("end_ts").alias("ts"), pl.col("qty").alias("d")]),
            bp.select([pl.col("cure_ts").alias("ts"), (-pl.col("qty")).alias("d")]),
        ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
        r2 = rec.filter(pl.col("plant") == p)
        # mould changes = campaigns whose press previously held another GT
        ch = c.sort(["press", "start_ts"]).with_columns(
            (pl.col("gt_code") != pl.col("gt_code").shift(1).over("press")).alias("m"))
        out[p] = dict(
            n_active=live / 744.0,
            campaigns=c.height,
            presses_per_gt=c.group_by("gt_code").agg(
                pl.col("press").n_unique()).mean()["press"][0],
            runs=runs.height,
            p05=np.percentile(q, 5), p50=np.percentile(q, 50),
            p95=np.percentile(q, 95), qmin=q.min(), qmax=q.max(),
            hrs=np.percentile(np.array(runs["h"], float), 50),
            chg=(runs.height - fp["machine"].n_unique()) / max(md, 1),
            below=100.0 * (q < FLOOR[p]).sum() / len(q),
            head=np.percentile(w, 50), head_max=w.max(),
            r5=int((w > 72).sum()),
            inv=float(ev["bal"].mean()),
            fed=100.0 * float(r2["qty_fed"].sum()) / max(float(r2["qty"].sum()), 1),
            tyres=float(bp["qty"].sum()),
            mould_ch=int(ch["m"].sum()),
        )
    return out


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    res = {}
    for tag, cluster in (("exp_base", False), ("exp_clust", True)):
        run = ROOT / "runs" / tag
        shutil.rmtree(run, ignore_errors=True)
        print(f"\n### {tag}  (cluster={cluster}) ###")
        run_l5(month, tag, cluster)
        for mod in ("l6_build_gate", "l7_pull_release"):
            o = sh(mod, month, tag)
            if "Traceback" in o:
                print(o[-1200:])
                raise SystemExit(f"{mod} failed for {tag}")
        res[tag] = measure(run)

    for p in ("PCR", "TBR"):
        print("\n" + "=" * 104)
        print(f"{p}")
        print("=" * 104)
        rows = [("n_active GTs", "n_active", "{:.1f}"),
                ("cure campaigns", "campaigns", "{:.0f}"),
                ("presses per GT", "presses_per_gt", "{:.1f}"),
                ("mould changes", "mould_ch", "{:.0f}"),
                ("build runs", "runs", "{:.0f}"),
                ("lot min", "qmin", "{:.0f}"),
                ("lot p05", "p05", "{:.0f}"),
                ("lot p50", "p50", "{:.0f}"),
                ("lot p95", "p95", "{:.0f}"),
                ("lot max", "qmax", "{:.0f}"),
                ("run hours p50", "hrs", "{:.2f}"),
                ("changeovers/mach-day", "chg", "{:.2f}"),
                ("% runs below floor", "below", "{:.1f}"),
                ("GT head p50 h", "head", "{:.2f}"),
                ("GT head max h", "head_max", "{:.2f}"),
                ("R5 breaches", "r5", "{:.0f}"),
                ("GT inventory", "inv", "{:,.0f}"),
                ("tyres built", "tyres", "{:,.0f}"),
                ("fulfilment %", "fed", "{:.1f}")]
        print(f"  {'metric':<24}{'baseline':>14}{'clustered':>14}{'plant':>14}")
        print("  " + "-" * 66)
        for label, key, fmt in rows:
            a = res["exp_base"].get(p, {}).get(key)
            b = res["exp_clust"].get(p, {}).get(key)
            pl_v = PLANT[p].get({"p50": "p50", "p05": "p05", "p95": "p95",
                                 "runs": "runs", "hrs": "hrs", "chg": "chg",
                                 "below": "below", "inv": "inv"}.get(key, ""), None)
            sa = fmt.format(a) if a is not None else "-"
            sb = fmt.format(b) if b is not None else "-"
            sp = fmt.format(pl_v) if pl_v is not None else "-"
            print(f"  {label:<24}{sa:>14}{sb:>14}{sp:>14}")


if __name__ == "__main__":
    main()
