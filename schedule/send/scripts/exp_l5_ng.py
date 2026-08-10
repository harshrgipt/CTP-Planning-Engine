"""EXPERIMENT: rate anchoring + campaign identity as an alternative L5.
Changes NO production code. Writes only its own run directory.

    PYTHONPATH=. python scripts/exp_l5_ng.py 2026-07

WHAT IS BEING TESTED  (formulation v2 s2.2 + s6.1, RULEBOOK s3b, MEMORY s424)

    n_g = ceil( W_g / (D_g * 24) )        rate anchoring -- presses for GT g
    ONE campaign per (GT, press), spanning the GT's whole window D_g

  v2 s2.2 reproduces the plant's changeover count exactly on both plants from
  `campaigns = SUM_g n_g`, and s6.1 reports corr(presses assigned, n_g) = 0.918
  PCR / 0.944 TBR. Our L5 instead places each L4.5 lot on whichever eligible
  press frees earliest, which yields 4.45 distinct presses per GT but only p50 2
  CONCURRENT against a mould cap of 4 -- so a GT stays live 592 h of 744 rather
  than the plant's ~408 h. That stretched window is what drives the wait gap.

  Emits `cure_campaigns.parquet` in L5's exact schema, so L6/L7/L10/L11 run
  downstream completely unmodified.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# plant window per GT (v2 s3 step 2): a GT runs hard for ~2 weeks, dormant after
D_TARGET_D = {"PCR": 15.6, "TBR": 17.1}


def build_campaigns(month: str, run: Path) -> dict:
    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet").filter(pl.col("n_lots") > 0)
    press = pl.read_parquet(D / f"cap_press_{month}.parquet")
    mould = pl.read_parquet(D / f"cap_mould_{month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")
    pmc = pl.read_parquet(D / "press_mould_change.parquet")

    rate_p, mch_p = {}, {}
    for p in ("PCR", "TBR"):
        c = cav.filter(pl.col("plant") == p)
        rate_p[p] = float(c["cavities"].median()) * 3600.0 / float(c["cycle_s"].median())
        mch_p[p] = float(pmc.filter(pl.col("plant") == p)["mould_change_min"].median()) / 60.0

    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ("PCR", "TBR")}
    bband = {p: float(P["campaign_bands"][p]["build"]["hours_p50"]) for p in ("PCR", "TBR")}

    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    H = ndays * 24.0

    elig: dict[tuple, list] = {}
    for r in press.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
    moulds = {(r["plant"], r["gt_code"]): max(int(r["moulds"]), 1)
              for r in mould.iter_rows(named=True)}

    # GT totals from L4.5 (same demand L5 sees)
    gts = []
    for r in lots.iter_rows(named=True):
        sizes = r.get("lot_sizes")
        n = float(sum(sizes)) if sizes is not None and len(sizes) else \
            float(r["lot_qty"]) * int(r["n_lots"])
        gts.append({"plant": r["plant"], "gt_code": r["gt_code"],
                    "mould_set": r["mould_set"], "N": n})

    busy: dict[str, list] = {}
    last_gt: dict[str, str] = {}
    placed, unplaced = [], []
    stats: dict = {}

    for plant in ("PCR", "TBR"):
        gl = [g for g in gts if g["plant"] == plant]
        rate = rate_p[plant]
        floor_ts = t0 + timedelta(hours=tau[plant] + bband[plant])
        ng_list = []
        # most constrained first: fewest eligible presses, then largest work
        for g in sorted(gl, key=lambda x: (len(elig.get((plant, x["gt_code"]), [])),
                                           -x["N"])):
            gt = g["gt_code"]
            cand = sorted(elig.get((plant, gt), []))
            if not cand:
                unplaced.append({**g, "reason": "no eligible press"})
                continue
            W = g["N"] / rate                       # press-hours of work
            cap = min(moulds.get((plant, gt), 1), len(cand))
            n_g = int(np.clip(np.ceil(W / (D_TARGET_D[plant] * 24.0)), 1, cap))
            dur_h = W / n_g                          # window given integer n_g
            mc = mch_p[plant]

            # earliest window where n_g eligible presses are simultaneously free
            best = None
            for start_h in range(0, int(H - dur_h) + 1, 6):     # 6 h grid
                s = max(floor_ts, t0 + timedelta(hours=start_h))
                e = s + timedelta(hours=dur_h)
                if e > t0 + timedelta(hours=H):
                    break
                free = []
                for pr in cand:
                    need_s = s - timedelta(hours=mc if last_gt.get(pr) not in (None, gt) else 0)
                    if all(need_s >= be or e <= bs for bs, be in busy.get(pr, [])):
                        free.append(pr)
                    if len(free) >= n_g:
                        break
                if len(free) >= n_g:
                    best = (s, e, free[:n_g])
                    break
            if best is None:
                unplaced.append({**g, "reason": "no common window"})
                continue
            s, e, prs = best
            q_each = g["N"] / len(prs)
            for pr in prs:
                busy.setdefault(pr, []).append((s, e))
                last_gt[pr] = gt
                placed.append({"plant": plant, "gt_code": gt,
                               "mould_set": g["mould_set"], "press": pr,
                               "start_ts": s, "end_ts": e,
                               "qty": round(q_each, 1),
                               "hours": round(dur_h, 2)})
            ng_list.append(n_g)
        stats[plant] = {"n_g_mean": float(np.mean(ng_list)) if ng_list else 0,
                        "campaigns": sum(ng_list), "gts": len(ng_list)}

    df = pl.DataFrame(placed)
    run.mkdir(parents=True, exist_ok=True)
    df.write_parquet(run / "cure_campaigns.parquet")
    up = pl.DataFrame(unplaced) if unplaced else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                "N": pl.Float64, "reason": pl.Utf8})
    up.select([c for c in ("plant", "gt_code", "mould_set", "reason") if c in up.columns]) \
      .with_columns(pl.lit(0.0).alias("qty"), pl.lit(0).alias("seq")) \
      .write_parquet(run / "cure_unplaced.parquet")
    stats["unplaced"] = len(unplaced)
    stats["placed_qty"] = float(df["qty"].sum()) if df.height else 0.0
    return stats


def sh(mod: str, month: str, run: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([PY, "-m", f"planner.cmbc.{mod}", "--month", month,
                        "--run", run], env=env, cwd=ROOT, capture_output=True, text=True)
    return r.stdout + r.stderr


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    run = ROOT / "runs" / "exp_ng"
    shutil.rmtree(run, ignore_errors=True)
    st = build_campaigns(month, run)
    print("ALT L5 (rate anchoring + campaign identity)")
    for p in ("PCR", "TBR"):
        if p in st:
            print(f"  {p}: n_g mean {st[p]['n_g_mean']:.2f} · "
                  f"campaigns {st[p]['campaigns']} (= SUM n_g) · GTs {st[p]['gts']}")
    print(f"  unplaced GTs {st['unplaced']} · placed {st['placed_qty']:,.0f} tyres\n")
    for mod in ("l6_build_gate", "l7_pull_release"):
        o = sh(mod, month, "exp_ng")
        if "Traceback" in o:
            print(o[-1500:])
            raise SystemExit(f"{mod} failed")
        if mod == "l7_pull_release":
            for line in o.splitlines():
                if any(k in line for k in ("BUILD RUNS", "plant ", "PCR ", "TBR ",
                                           "GT INVENTORY", "TRUE FULFIL", "R5",
                                           "machines/GT", "predicted")):
                    print(line)


if __name__ == "__main__":
    main()
