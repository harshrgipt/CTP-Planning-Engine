"""EXPERIMENT: n_g concurrent presses per GT, split into k campaigns.
Read-only w.r.t. production code. Writes only its own run directory.

    PYTHONPATH=. python scripts/exp_l5_split.py 2026-07

WHY
  n_g (concurrent presses on a GT) is THE inventory lever:
      W = tau* + (Q/2)(1/r - 1/b),   r = n_g x press_rate
  Solving the plant's observed W=8.84 h for r gives 22.3 tyres/h = 3.25 presses
  -- matching its independently measured n_g of 3.28-3.42. Ours implies 1.99.

  `exp_l5_ng.py` set n_g correctly and got inventory 3,876 (below the plant's
  4,772) but only 65.9% fulfilment, because it demanded ONE rectangle per
  (GT, press) of width W_g/n_g ~ 354 h. That hole does not exist at 95% press
  utilisation.

  KEY ASYMMETRY: n_g is a WITHIN-CAMPAIGN property. It survives fragmentation in
  time as long as the presses inside a campaign run concurrently. So split the
  rectangle into k campaigns of width W_g/(n_g x k) and keep the drain rate.

LADDER -- degrade n_g before k. Campaign count hurts changeovers linearly;
n_g hurts inventory only through 1/r, and exp_ng showed ~0.3 of n_g of margin
before plant parity. Only widen k when n_g has already been given up.

UNEVEN SPLIT -- inventory is driven by the LAST unit's wait, so put the bulk
against the earliest deadlines and let a small trailing campaign sweep the tail.
The small block is also the one that has to fit at 95% utilisation, and small
fits.
"""
from __future__ import annotations

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

# (k, n_g scale) in degradation order: n_g down before k up.
LADDER = [(2, 1.00), (2, 0.90), (3, 1.00), (3, 0.90),
          (4, 1.00), (4, 0.90), (6, 1.00), (8, 1.00)]
SPLITS = {2: [0.65, 0.35], 3: [0.50, 0.30, 0.20],
          4: [0.40, 0.25, 0.20, 0.15],
          6: [0.28, 0.20, 0.16, 0.14, 0.12, 0.10],
          8: [0.22, 0.16, 0.13, 0.12, 0.10, 0.10, 0.09, 0.08]}
D_TARGET_D = {"PCR": 15.6, "TBR": 17.1}


def free_at(busy: dict, prs: list, s: datetime, e: datetime) -> bool:
    return all(all(e <= bs or s >= be for bs, be in busy.get(pr, []))
               for pr in prs)


def build(month: str, run: Path) -> dict:
    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet").filter(pl.col("n_lots") > 0)
    press = pl.read_parquet(D / f"cap_press_{month}.parquet")
    mould = pl.read_parquet(D / f"cap_mould_{month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")
    import json
    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ("PCR", "TBR")}
    bband = {p: float(P["campaign_bands"][p]["build"]["hours_p50"]) for p in ("PCR", "TBR")}

    rate_p = {}
    for p in ("PCR", "TBR"):
        c = cav.filter(pl.col("plant") == p)
        rate_p[p] = float(c["cavities"].median()) * 3600.0 / float(c["cycle_s"].median())

    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, 7, 0)
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    H = ndays * 24.0

    elig: dict = {}
    for r in press.iter_rows(named=True):
        elig.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
    moulds = {(r["plant"], r["gt_code"]): max(int(r["moulds"]), 1)
              for r in mould.iter_rows(named=True)}

    gts = []
    for r in lots.iter_rows(named=True):
        s = r.get("lot_sizes")
        n = float(sum(s)) if s is not None and len(s) else float(r["lot_qty"]) * int(r["n_lots"])
        gts.append({"plant": r["plant"], "gt_code": r["gt_code"],
                    "mould_set": r["mould_set"], "N": n})

    busy: dict = {}
    placed, unplaced, chosen = [], [], []

    for plant in ("PCR", "TBR"):
        gl = [g for g in gts if g["plant"] == plant]
        rate = rate_p[plant]
        floor_ts = t0 + timedelta(hours=tau[plant] + bband[plant])
        # FFD: widest simultaneous demand first -- n_g x campaign width
        def hardness(g):
            W = g["N"] / rate
            cap = min(moulds.get((plant, g["gt_code"]), 1),
                      len(elig.get((plant, g["gt_code"]), [])) or 1)
            ng = max(1, min(cap, int(np.ceil(W / (D_TARGET_D[plant] * 24.0)))))
            return -(ng * (W / max(ng, 1)))
        for g in sorted(gl, key=hardness):
            gt = g["gt_code"]
            cand = sorted(elig.get((plant, gt), []))
            if not cand:
                unplaced.append({**g, "reason": "no eligible press"})
                continue
            W = g["N"] / rate
            cap = min(moulds.get((plant, gt), 1), len(cand))
            ng0 = max(1, min(cap, int(np.ceil(W / (D_TARGET_D[plant] * 24.0)))))

            done = False
            for k, scale in LADDER:
                ng = max(1, int(round(ng0 * scale)))
                shares = SPLITS[k]
                widths = [W * sh / ng for sh in shares]
                if sum(widths) > H:
                    continue
                # campaign j is aimed at its share of the horizon so the GT's
                # cure is spread, not front-loaded
                slot, ok = [], True
                cursor = floor_ts
                for j, (sh, w) in enumerate(zip(shares, widths)):
                    aim = t0 + timedelta(hours=H * sum(shares[:j]))
                    start = max(cursor, aim, floor_ts)
                    found = None
                    step = max(1.0, w / 8.0)
                    t = start
                    while t + timedelta(hours=w) <= t0 + timedelta(hours=H):
                        free = [pr for pr in cand
                                if free_at(busy, [pr], t, t + timedelta(hours=w))]
                        if len(free) >= ng:
                            found = (t, t + timedelta(hours=w), free[:ng])
                            break
                        t += timedelta(hours=step)
                    if found is None:
                        ok = False
                        break
                    slot.append((found, sh))
                    cursor = found[1]
                if ok:
                    for (s, e, prs), sh in slot:
                        q = g["N"] * sh / len(prs)
                        for pr in prs:
                            busy.setdefault(pr, []).append((s, e))
                            placed.append({"plant": plant, "gt_code": gt,
                                           "mould_set": g["mould_set"], "press": pr,
                                           "start_ts": s, "end_ts": e,
                                           "qty": round(q, 1),
                                           "hours": round((e - s).total_seconds() / 3600, 2)})
                    chosen.append({"plant": plant, "gt": gt, "k": k, "n_g": ng,
                                   "n_g0": ng0})
                    done = True
                    break
            if not done:
                unplaced.append({**g, "reason": "ladder exhausted"})

    df = pl.DataFrame(placed)
    run.mkdir(parents=True, exist_ok=True)
    df.write_parquet(run / "cure_campaigns.parquet")
    pl.DataFrame({"plant": [], "gt_code": [], "mould_set": [], "qty": [],
                  "seq": [], "reason": []},
                 schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                         "qty": pl.Float64, "seq": pl.Int64, "reason": pl.Utf8}
                 ).write_parquet(run / "cure_unplaced.parquet")
    ch = pl.DataFrame(chosen) if chosen else pl.DataFrame()
    return {"placed_qty": float(df["qty"].sum()) if df.height else 0.0,
            "unplaced": len(unplaced), "chosen": ch,
            "campaigns": df.height}


def sh_run(mod: str, month: str, run: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([PY, "-m", f"planner.cmbc.{mod}", "--month", month,
                        "--run", run], env=env, cwd=ROOT, capture_output=True, text=True)
    return r.stdout + r.stderr


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    run = ROOT / "runs" / "exp_split"
    shutil.rmtree(run, ignore_errors=True)
    st = build(month, run)
    ch = st["chosen"]
    print("ALT L5 -- n_g concurrent presses, split into k campaigns")
    if ch.height:
        for p in ("PCR", "TBR"):
            c = ch.filter(pl.col("plant") == p)
            if not c.height:
                continue
            mix = c.group_by("k").len().sort("k")
            print(f"  {p}: {c.height} GTs · realised n_g mean {float(c['n_g'].mean()):.2f} "
                  f"(requested {float(c['n_g0'].mean()):.2f}) · k mix "
                  + " ".join(f"k{r['k']}:{r['len']}" for r in mix.iter_rows(named=True)))
    print(f"  campaigns {st['campaigns']} · unplaced GTs {st['unplaced']} · "
          f"placed {st['placed_qty']:,.0f} tyres\n")
    for mod in ("l6_build_gate", "l7_pull_release"):
        o = sh_run(mod, month, "exp_split")
        if "Traceback" in o:
            print(o[-1200:])
            raise SystemExit(f"{mod} failed")
        if mod == "l7_pull_release":
            for line in o.splitlines():
                if any(x in line for x in ("TRUE FULFIL", "GT INVENTORY", "predicted",
                                           "BUILD RUNS", "chg/mach", "R5", "machines/GT")):
                    print(line)
            for line in o.splitlines():
                if line.strip().startswith(("PCR", "TBR")) and "|" not in line:
                    print(line)


if __name__ == "__main__":
    main()
