"""L5-NG -- CURE CAMPAIGN MASTER by MOULD-ANCHORED ADDITIVE SEATING.

    python -m planner.cmbc.l5_ng --month 2026-07 --out <run>

Drop-in alternative to `l5_cure_master`: emits `cure_campaigns.parquet` in the
same schema, so L6/L7/L10/L11 run downstream unmodified.

THE LEVER
    I = lambda x W,   W = tau* + (Q/2)(1/r - 1/b),   r = n_g x press_rate
`n_g` -- presses concurrently drawing one GT -- is the only term that lowers
inventory without shrinking the lot. Solving the plant's observed W = 8.84 h for
r gives 22.3 tyres/h = 3.25 presses, matching its independently measured n_g of
3.28-3.42. So n_g is ANCHORED TO THE PLANT (3.25 PCR / 3.11 TBR), not derived
from a window target -- deriving it gave 4.4-5.4, which packs far worse for no
inventory gain we could not get more cheaply.

ADDITIVE, NEVER CONJUNCTIVE -- the rule this codebase paid for six times
    Requiring n_g presses to be free for a COMMON window is a conjunctive
    constraint and costs 20-35 % of demand at 95 % press utilisation:
        rigid rectangle per (GT, press)      65.9 % fulfilment
        split into k campaigns               76.8 %
        ragged additive seating              89.2 %
    Time-averaged concurrency is W_g / window, so a GT needs W_g press-hours
    delivered inside a window of length W_g/n_g -- it does NOT need the presses
    aligned. Seat them one at a time, each into its own free time, and
    accumulate until the integral is met. Conjunctive -> additive.

NO DISPERSION FLOOR
    A minimum instantaneous concurrency was swept at {0, .4, .6, .8} x n_g.
    Floor 0 gave the best fulfilment (89.2 %) AND inventory already under the
    plant (4,361 vs 4,772). The guard is not load-bearing; it only costs
    fulfilment. Do not reintroduce it without new evidence.

LADDER
    Window L = W_g/n_g, so lowering n_g IS lengthening the window -- one knob.
    Degrade n_g downward only; it makes packing strictly easier.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from planner.config import CONFIG

ROOT = Path(__file__).resolve().parent.parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"

# Plant-measured concurrent presses per GT. NOT derived from a window target.
NG_TARGET = {"PCR": float(os.environ.get("PLANNER_NG_PCR", "3.25")),
             "TBR": float(os.environ.get("PLANNER_NG_TBR", "3.11"))}
# Ladder goes UP: a congested schedule absorbs a SHORTER window more easily, and
# L = W_g/n_g shortens as n_g rises. Bounded by the mould cap.
NG_LADDER = [1.00, 1.30, 1.70, 2.20, 3.00, 4.00]
STEP_H = 8.0                      # window-start scan granularity


def free_slots(busy: list, a: datetime, b: datetime) -> list:
    """Free sub-intervals of [a, b] given a press's busy intervals."""
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet").filter(
        pl.col("n_lots") > 0)
    press = pl.read_parquet(D / f"cap_press_{a.month}.parquet")
    mould = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    cav = pl.read_parquet(D / "l3_cavities.parquet")

    tau = {p: float(P["tau"][p]["tau_star_h"]) for p in ("PCR", "TBR")}
    bband = {p: float(P["campaign_bands"][p]["build"]["hours_p50"])
             for p in ("PCR", "TBR")}
    rate = {}
    for p in ("PCR", "TBR"):
        c = cav.filter(pl.col("plant") == p)
        rate[p] = float(c["cavities"].median()) * 3600.0 / float(c["cycle_s"].median())

    y, m = int(a.month[:4]), int(a.month[5:7])
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
        n = (float(sum(s)) if s is not None and len(s)
             else float(r["lot_qty"]) * int(r["n_lots"]))
        gts.append({"plant": r["plant"], "gt_code": r["gt_code"],
                    "mould_set": r["mould_set"], "N": n})

    busy: dict = {}
    placed, unplaced, chosen = [], [], []

    print("=" * 92)
    print(f"L5-NG  MOULD-ANCHORED ADDITIVE SEATING  --  {a.month}")
    print("=" * 92)
    print(f"  n_g anchored to the plant: PCR {NG_TARGET['PCR']:.2f} · "
          f"TBR {NG_TARGET['TBR']:.2f}   (additive, no dispersion floor)\n")

    for plant in ("PCR", "TBR"):
        rt = rate[plant]
        floor_ts = t0 + timedelta(hours=tau[plant] + bband[plant])
        gl = [g for g in gts if g["plant"] == plant]

        # FFD: the GTs demanding the widest simultaneous press block go first,
        # into an empty schedule.
        def hard(g):
            W = g["N"] / rt
            cap = min(moulds.get((plant, g["gt_code"]), 1),
                      len(elig.get((plant, g["gt_code"]), [])) or 1)
            return -min(cap, NG_TARGET[plant]) * W

        for g in sorted(gl, key=hard):
            gt = g["gt_code"]
            cand = sorted(elig.get((plant, gt), []))
            if not cand:
                unplaced.append({**g, "reason": "no eligible press"})
                continue
            W = g["N"] / rt                       # press-hours of work
            cap = min(moulds.get((plant, gt), 1), len(cand))
            # n_g HAS A HARD FLOOR: the window L = W_g/n_g must fit in the
            # USABLE horizon (H less the tau* + band start floor), so
            #     n_g >= W_g / (H - floor_h)
            # The plant's 3.25 is an AVERAGE. A 55,224-tyre GT needs 11.85
            # presses just to finish inside the month; anchoring it to 3.25 and
            # then degrading DOWNWARD lengthens L and every rung fails -- that
            # stranded 11 PCR GTs carrying 256,790 tyres.
            usable = H - (tau[plant] + bband[plant])
            ng_min = W / usable
            ng0 = max(1.0, min(float(cap), max(NG_TARGET[plant], ng_min)))

            done = False
            for scale in NG_LADDER:
                # Ladder goes UP, not down: more presses => SHORTER window, and a
                # shorter window is what a congested schedule can still absorb.
                # Bounded by the mould cap, which is the physical limit.
                ng = min(float(cap), max(ng_min, ng0 * scale))
                L = W / ng                        # window giving concurrency ng
                if L > usable:
                    continue
                t = floor_ts
                while t + timedelta(hours=L) <= t0 + timedelta(hours=H):
                    lo, hi = t, t + timedelta(hours=L)
                    got, acc = [], 0.0
                    for pr in cand:               # ADDITIVE: accumulate the integral
                        if acc >= W:
                            break
                        for (s, e) in free_slots(busy.get(pr, []), lo, hi):
                            if acc >= W:
                                break
                            take = min((e - s).total_seconds() / 3600.0, W - acc)
                            if take < 0.5:
                                continue
                            got.append((pr, s, s + timedelta(hours=take)))
                            acc += take
                    if acc >= W - 1e-6:
                        for pr, s, e in got:
                            hrs = (e - s).total_seconds() / 3600.0
                            busy.setdefault(pr, []).append((s, e))
                            placed.append({
                                "plant": plant, "gt_code": gt,
                                "mould_set": g["mould_set"], "press": pr,
                                "start_ts": s, "end_ts": e,
                                "qty": round(hrs * rt, 1),
                                "hours": round(hrs, 2)})
                        chosen.append({"plant": plant, "gt": gt,
                                       "ng_req": ng0, "ng_real": W / L,
                                       "blocks": len(got)})
                        done = True
                        break
                    t += timedelta(hours=STEP_H)
                if done:
                    break
            if not done:
                unplaced.append({**g, "reason": "ladder exhausted"})

    df = pl.DataFrame(placed)
    run = ROOT / "runs" / (a.out or f"cmbc_{a.month}")
    run.mkdir(parents=True, exist_ok=True)
    df.write_parquet(run / "cure_campaigns.parquet")
    (pl.DataFrame(unplaced) if unplaced else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "mould_set": pl.Utf8,
                "N": pl.Float64, "reason": pl.Utf8})
     ).write_parquet(run / "cure_unplaced.parquet")
    pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                         "mould_set": pl.Utf8, "seq": pl.Int64, "press": pl.Utf8,
                         "qty": pl.Float64, "carry_from": pl.Datetime,
                         "ends": pl.Datetime}
                 ).write_parquet(run / "carry_out.parquet")

    ch = pl.DataFrame(chosen) if chosen else pl.DataFrame()
    print(f"  {'plant':<6}{'GTs':>5}{'campaigns':>11}{'tyres':>11}"
          f"{'n_g real':>10}{'presses':>9}")
    for p in ("PCR", "TBR"):
        d = df.filter(pl.col("plant") == p)
        c = ch.filter(pl.col("plant") == p) if ch.height else ch
        if not d.height:
            continue
        print(f"  {p:<6}{d['gt_code'].n_unique():>5}{d.height:>11,}"
              f"{int(d['qty'].sum()):>11,}"
              f"{(float(c['ng_real'].mean()) if c.height else 0):>10.2f}"
              f"{d['press'].n_unique():>9}")
    print(f"\n  unplaced GTs: {len(unplaced)}")
    for p in ("PCR", "TBR"):
        n = sum(1 for u in unplaced if u["plant"] == p)
        if n:
            print(f"    {p}: {n} ({sum(u['N'] for u in unplaced if u['plant']==p):,.0f} tyres)")
    print(f"\n  -> {run}/cure_campaigns.parquet")


if __name__ == "__main__":
    main()
