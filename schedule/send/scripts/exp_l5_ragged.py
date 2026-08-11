"""EXPERIMENT: RAGGED concurrency seating. Read-only w.r.t. production code.

    PYTHONPATH=. python scripts/exp_l5_ragged.py 2026-07 [floor ...]

THE REFORMULATION
  Every previous attempt demanded n_g presses free for a COMMON window -- a
  conjunctive constraint. Four runs in a row it cost 20-35% of demand at 95%
  press utilisation (rigid 65.9%, split 76.8%).

  The physics only needs the DRAIN RATE r = n_g x press_rate, and
      time-averaged concurrency  =  W_g / window_length
  so a GT needs W_g press-hours delivered inside a window of length W_g/n_g --
  it does NOT need the presses aligned. Seat them one at a time, each into its
  own free time inside the window, and accumulate until the integral is met.
  Conjunctive -> additive. Additive constraints pack at high utilisation.

THE DISPERSION GUARD
  W_g/window hits n_g just as well with 6 presses for half the window and 0.6 for
  the other half -- same integral, much worse drain, because W is set by the
  trailing units. So require min instantaneous concurrency over the window's
  middle 80% to be >= floor x n_g. The floor is the ONE parameter here with no
  measurement behind it, so it is swept, not fixed.

LADDER
  Window length L = W_g/n_g, so relaxing n_g IS extending the window -- they are
  the same knob. Ladder is therefore n_g scale only; the floor is held fixed
  within a run so the sweep reads cleanly.
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
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

NG_LADDER = [1.00, 0.90, 0.80, 0.65, 0.50, 0.35]
D_TARGET_D = {"PCR": 15.6, "TBR": 17.1}
STEP_H = 8.0                      # window start scan granularity


def free_slots(busy: list, a: datetime, b: datetime) -> list:
    """Free sub-intervals of [a,b] given sorted busy intervals."""
    out, cur = [], a
    for s, e in sorted(busy):
        if e <= a or s >= b:
            continue
        if s > cur:
            out.append((cur, min(s, b)))
        cur = max(cur, e)
        if cur >= b:
            break
    if cur < b:
        out.append((cur, b))
    return [(s, e) for s, e in out if (e - s).total_seconds() > 900]


def min_conc(iv: list, a: datetime, b: datetime) -> float:
    """Min instantaneous concurrency over the middle 80% of [a,b]."""
    span = (b - a).total_seconds()
    lo = a + timedelta(seconds=span * 0.1)
    hi = b - timedelta(seconds=span * 0.1)
    pts = sorted({lo, hi} | {t for s, e in iv for t in (s, e) if lo <= t <= hi})
    if len(pts) < 2:
        return 0.0
    m = 1e9
    for i in range(len(pts) - 1):
        mid = pts[i] + (pts[i + 1] - pts[i]) / 2
        m = min(m, sum(1 for s, e in iv if s <= mid < e))
    return float(m)


def build(month: str, run: Path, floor: float) -> dict:
    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet").filter(pl.col("n_lots") > 0)
    press = pl.read_parquet(D / f"cap_press_{month}.parquet")
    mould = pl.read_parquet(D / f"cap_mould_{month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")
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
        rate = rate_p[plant]
        floor_ts = t0 + timedelta(hours=tau[plant] + bband[plant])
        gl = [g for g in gts if g["plant"] == plant]

        def hard(g):                     # FFD: widest simultaneous demand first
            W = g["N"] / rate
            cap = min(moulds.get((plant, g["gt_code"]), 1),
                      len(elig.get((plant, g["gt_code"]), [])) or 1)
            return -min(cap, max(1, int(np.ceil(W / (D_TARGET_D[plant] * 24.0)))))

        for g in sorted(gl, key=hard):
            gt = g["gt_code"]
            cand = sorted(elig.get((plant, gt), []))
            if not cand:
                unplaced.append({**g, "reason": "no eligible press"})
                continue
            W = g["N"] / rate
            cap = min(moulds.get((plant, gt), 1), len(cand))
            ng0 = max(1, min(cap, int(np.ceil(W / (D_TARGET_D[plant] * 24.0)))))

            done = False
            for scale in NG_LADDER:
                ng = max(1.0, ng0 * scale)
                L = W / ng                       # window that yields concurrency ng
                if L > H:
                    continue
                t = floor_ts
                while t + timedelta(hours=L) <= t0 + timedelta(hours=H):
                    a, b = t, t + timedelta(hours=L)
                    # ADDITIVE: take each press's free time inside the window,
                    # largest first, until the press-hour integral is met.
                    got, acc = [], 0.0
                    for pr in cand:
                        if acc >= W:
                            break
                        for (s, e) in free_slots(busy.get(pr, []), a, b):
                            if acc >= W:
                                break
                            take = min((e - s).total_seconds() / 3600.0, W - acc)
                            if take < 0.5:
                                continue
                            got.append((pr, s, s + timedelta(hours=take)))
                            acc += take
                    if acc >= W - 1e-6 and (floor <= 0 or
                                            min_conc([(s, e) for _, s, e in got], a, b)
                                            >= floor * ng):
                        for pr, s, e in got:
                            hrs = (e - s).total_seconds() / 3600.0
                            busy.setdefault(pr, []).append((s, e))
                            placed.append({"plant": plant, "gt_code": gt,
                                           "mould_set": g["mould_set"], "press": pr,
                                           "start_ts": s, "end_ts": e,
                                           "qty": round(hrs * rate, 1),
                                           "hours": round(hrs, 2)})
                        chosen.append({"plant": plant, "gt": gt, "ng_req": ng0,
                                       "ng_real": W / L, "blocks": len(got)})
                        done = True
                        break
                    t += timedelta(hours=STEP_H)
                if done:
                    break
            if not done:
                unplaced.append({**g, "reason": "ladder exhausted"})

    df = pl.DataFrame(placed)
    run.mkdir(parents=True, exist_ok=True)
    df.write_parquet(run / "cure_campaigns.parquet")
    pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                         "qty": pl.Float64, "seq": pl.Int64, "reason": pl.Utf8}
                 ).write_parquet(run / "cure_unplaced.parquet")
    return {"qty": float(df["qty"].sum()) if df.height else 0.0,
            "unplaced": len(unplaced), "rows": df.height,
            "chosen": pl.DataFrame(chosen) if chosen else pl.DataFrame()}


def sh(mod, month, run):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([PY, "-m", f"planner.cmbc.{mod}", "--month", month,
                        "--run", run], env=env, cwd=ROOT, capture_output=True, text=True)
    return r.stdout + r.stderr


SAME = {"PCR": 11.3, "TBR": 10.0}          # measured plant July, minutes
DIFF = {"PCR": 42.4, "TBR": 24.0}


def score(run: Path, rim: dict) -> dict:
    b = pl.read_parquet(run / "build_schedule.parquet")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    f = b.filter(pl.col("machine") != "OPENING_STOCK")
    out = {"ful": 100 * float(rec["qty_fed"].sum()) / 491630}
    for p in ("PCR", "TBR"):
        fp, bp = f.filter(pl.col("plant") == p), b.filter(pl.col("plant") == p)
        if not fp.height:
            continue
        r = (fp.group_by(["machine", "run_id"])
             .agg(pl.col("gt_code").first(), pl.col("qty").sum().alias("q"),
                  pl.col("start_ts").min().alias("t"))
             .sort(["machine", "t"])
             .with_columns(pl.col("gt_code").shift(1).over("machine").alias("prev")))
        ch = r.filter(pl.col("prev").is_not_null())
        same = sum(1 for x in ch.iter_rows(named=True)
                   if rim.get(x["gt_code"]) == rim.get(x["prev"]))
        md = (fp.with_columns(pl.col("start_ts").dt.date().alias("d"))
              .select(["machine", "d"]).unique().height)
        ev = pl.concat([bp.select([pl.col("end_ts").alias("ts"), pl.col("qty").alias("d")]),
                        bp.select([pl.col("cure_ts").alias("ts"), (-pl.col("qty")).alias("d")])
                        ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
        w = np.array(bp["wait_h"], float)
        out[p] = {"count": ch.height, "same": 100 * same / max(ch.height, 1),
                  "wh": (same * SAME[p] + (ch.height - same) * DIFF[p]) / 60,
                  "inv": float(ev["bal"].mean()), "lot": float(r["q"].median()),
                  "W": w.mean(), "md": md}
    return out


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    floors = [float(x) for x in sys.argv[2:]] or [0.0, 0.4, 0.6, 0.8]
    sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
           if r.get("gt_code") and r.get("rim")}
    print(f"{'floor':>6}{'ng real':>9}{'unpl':>6}{'FULFIL':>9}"
          f"{'PCR inv':>9}{'PCR same%':>11}{'PCR wt-h':>10}{'PCR cnt':>9}{'PCR lot':>9}")
    for fl in floors:
        run = ROOT / "runs" / f"rag_{fl:g}"
        shutil.rmtree(run, ignore_errors=True)
        st = build(month, run, fl)
        for mod in ("l6_build_gate", "l7_pull_release"):
            o = sh(mod, month, run.name)
            if "Traceback" in o:
                print(f"  floor {fl}: {mod} FAILED"); print(o[-800:]); break
        else:
            s = score(run, rim)
            c = st["chosen"]
            ngr = float(c.filter(pl.col("plant") == "PCR")["ng_real"].mean()) if c.height else 0
            P = s.get("PCR", {})
            print(f"{fl:>6.1f}{ngr:>9.2f}{st['unplaced']:>6}{s['ful']:>8.1f}%"
                  f"{P.get('inv',0):>9,.0f}{P.get('same',0):>10.1f}%{P.get('wh',0):>10,.0f}"
                  f"{P.get('count',0):>9,}{P.get('lot',0):>9.0f}")
    print(f"\n{'plant':>6}{3.25:>9.2f}{'-':>6}{'-':>9}{4772:>9,}{91.8:>10.1f}%{171:>10,}{742:>9,}{363:>9}")


if __name__ == "__main__":
    main()
