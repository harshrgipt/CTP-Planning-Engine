"""FEASIBILITY CHECK for wave-scheduled cure campaigns. READ-ONLY.

    PYTHONPATH=. python scripts/check_wave_feasibility.py 2026-07

Answers one question before any layer is changed: can L5 be made to run FEWER
GTs concurrently, and does that actually deliver the plant's lot size and GT
inventory? Writes nothing -- no run directory, no warehouse file.

THE MODEL
    I  =  lambda*tau*  +  lambda*T/2            Little + the Q/2 sawtooth
    Q  =  (lambda / n_active) * T               lot = per-GT rate x time supply
      =>  I  =  lambda*tau*  +  Q*n_active/2

Inventory does not depend on concurrency; lot size does. So a big lot at low
inventory is reachable ONLY by cutting n_active. This script tests whether the
moulds, the press eligibility and the shared-mould-set exclusions allow it.

THE ONE-PARAMETER FAMILY
    presses_g = alpha * cap_g,  cap_g = min(moulds_g, eligible presses_g)
    live_g    = P_g / presses_g          (press-hours / presses)
    n_active  = sum(live_g) / horizon
Maxing out moulds (alpha=1) gives the lowest reachable concurrency. alpha is
therefore the single dial between "as the plant runs" and "as we run now".
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"
HORIZON_H = 744.0

TAU = {"PCR": 4.32, "TBR": 4.81}          # L0 measured
Q_PLANT = {"PCR": 363, "TBR": 86}         # plant lot p50
I_BAND = {"PCR": (4500, 4800), "TBR": (1200, 1500)}
I_PLANT = {"PCR": 4772, "TBR": 1743}      # measured plant GT inventory
CAMP_BAND = {"PCR": (40.0, 75.0), "TBR": (200.0, 330.0)}


def pack(gts: list, n_press: int, band: tuple) -> tuple[float, int, int, float]:
    """Place every GT's campaigns; return (peak presses, unplaced, conflicts, camp_h).

    A GT holds `presses_g` presses for `live_g` hours in TOTAL, but it does not
    hold them in one block: the plant runs a GT in repeated campaigns of 40-75 h
    (PCR) / 200-330 h (TBR). Packing one long block per GT is both unrealistic
    and much harder to fit -- big rigid pieces at 95% utilisation leave holes
    nothing fills. Split the live time into band-length campaigns first.

    GTs sharing a mould set may not overlap, and a GT may not overlap itself:
    one physical mould cannot be in two presses at once. Earliest-fit on a 1 h
    grid, widest campaigns first.
    """
    w_target = 0.5 * (band[0] + band[1])
    pieces = []
    for g in gts:
        n_c = max(1, int(round(g["live"] / w_target)))
        w = g["live"] / n_c
        for i in range(n_c):
            # PHASE the campaigns across the horizon. Earliest-fit puts all of a
            # GT's campaigns at the front where they collide with each other --
            # that is a packer artifact, not an infeasibility. A GT rebuilt n_c
            # times in a month should be spaced 744/n_c apart, which is also
            # what makes the replenishment interval T meaningful.
            pieces.append({"presses": g["presses"], "live": w,
                           "key": g["mould_set"],
                           "target": i * HORIZON_H / n_c})
    grid = np.zeros(int(HORIZON_H) + 2)
    busy: dict[str, list] = {}
    unplaced = conflicts = 0
    for pc in sorted(pieces, key=lambda x: (-x["presses"] * x["live"],)):
        dur = max(1, int(round(pc["live"])))
        hi = int(HORIZON_H) - dur
        if hi < 0:
            unplaced += 1
            continue
        # search outward from the phase target, so campaigns stay spread
        t0 = min(max(int(pc["target"]), 0), hi)
        order = sorted(range(0, hi + 1), key=lambda s: (abs(s - t0), s))
        placed = False
        for start in order:
            end = start + dur
            if grid[start:end].max() + pc["presses"] > n_press:
                continue
            if any(not (end <= s or start >= e) for s, e in busy.get(pc["key"], [])):
                continue
            grid[start:end] += pc["presses"]
            busy.setdefault(pc["key"], []).append((start, end))
            placed = True
            break
        if not placed:
            unplaced += 1
            if busy.get(pc["key"]):
                conflicts += 1
    camp_h = float(np.mean([p["live"] for p in pieces])) if pieces else 0.0
    return float(grid.max()), unplaced, conflicts, camp_h


def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    run = ROOT / "runs" / "july_cmbc_v5"
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    mould = pl.read_parquet(D / f"cap_mould_{month}.parquet")
    elig = pl.read_parquet(D / f"cap_press_{month}.parquet")
    lots = pl.read_parquet(D / f"l45_lots_{month}.parquet")

    for plant in ("PCR", "TBR"):
        c = camp.filter(pl.col("plant") == plant)
        if not c.height:
            continue
        n_press = c["press"].n_unique()
        lam = float(c["qty"].sum()) / HORIZON_H

        g = (c.group_by("gt_code")
             .agg(pl.col("hours").sum().alias("P"), pl.col("qty").sum().alias("q"))
             .join(mould.filter(pl.col("plant") == plant)
                   .select(["gt_code", "moulds"]), on="gt_code", how="left")
             .join(elig.filter(pl.col("plant") == plant)
                   .group_by("gt_code").len().rename({"len": "n_elig"}),
                   on="gt_code", how="left")
             .join(lots.filter(pl.col("plant") == plant)
                   .select(["gt_code", "mould_set"]), on="gt_code", how="left")
             .with_columns(pl.col("moulds").fill_null(1),
                           pl.col("n_elig").fill_null(1),
                           pl.col("mould_set").fill_null(pl.col("gt_code"))))
        g = g.with_columns(
            pl.min_horizontal("moulds", "n_elig").clip(lower_bound=1).alias("cap"))

        print("=" * 96)
        print(f"{plant}   lambda {lam:,.0f} tyres/h · {g.height} GTs · {n_press} presses "
              f"· {float(g['P'].sum()):,.0f} press-h of {n_press*HORIZON_H:,.0f} "
              f"({100*float(g['P'].sum())/(n_press*HORIZON_H):.1f}%)")
        print(f"  mould sets {g['mould_set'].n_unique()} · shared "
              f"{g.group_by('mould_set').len().filter(pl.col('len') > 1).height}")

        # what the targets imply
        T_t = 2.0 * (0.5 * sum(I_BAND[plant]) / lam - TAU[plant])
        n_t = lam * T_t / Q_PLANT[plant]
        print(f"  TARGET: I {0.5*sum(I_BAND[plant]):,.0f} -> T {T_t:.2f} h ; "
              f"Q {Q_PLANT[plant]} -> n_active {n_t:.1f}")

        print(f"\n  {'alpha':>6}{'presses/GT':>12}{'n_active':>10}{'live/GT':>9}"
              f"{'camp h':>8}{'T (h)':>7}{'Q':>7}{'I':>8}{'peak':>7}{'over':>6}{'conf':>6}")
        print("  " + "-" * 88)
        for alpha in (1.0, 0.85, 0.7, 0.55, 0.4, 0.25):
            pr = np.maximum(1.0, np.round(alpha * g["cap"].to_numpy()))
            pr = np.minimum(pr, g["cap"].to_numpy())
            live = g["P"].to_numpy() / pr
            n_act = live.sum() / HORIZON_H
            # per-GT draw rate while live, then the lot at the target interval
            T = T_t
            q_lot = (lam / n_act) * T
            inv = lam * TAU[plant] + lam * T / 2.0
            # campaign length if the GT's live time is split into whole campaigns
            rows = [{"presses": int(p), "live": float(l), "mould_set": ms}
                    for p, l, ms in zip(pr, live, g["mould_set"].to_list())]
            peak, over, conf, camp_h = pack(rows, n_press, CAMP_BAND[plant])
            print(f"  {alpha:>6.2f}{pr.mean():>12.1f}{n_act:>10.1f}"
                  f"{live.mean():>9.0f}{camp_h:>8.0f}{T:>7.2f}{q_lot:>7.0f}"
                  f"{inv:>8,.0f}{peak:>7.0f}{over:>6}{conf:>6}")

        print(f"\n  plant reference: Q {Q_PLANT[plant]} · I {I_PLANT[plant]:,} · "
              f"campaign band {CAMP_BAND[plant][0]:.0f}-{CAMP_BAND[plant][1]:.0f} h")


if __name__ == "__main__":
    main()
