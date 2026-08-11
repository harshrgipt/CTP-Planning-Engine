"""RULEBOOK SCORECARD -- every business rule measured PLANT vs OURS, side by side.

    PYTHONPATH=. python scripts/rulebook_scorecard.py runs/v19 [2026-07]

Read-only. Plant figures come from `v_build` / `v_curing` for the SAME month, so
both columns are the same arithmetic on the same definition -- the only honest
way to compare. Where a rule has no plant analogue (a constraint we impose on
ourselves) the plant column shows "--".

DEFINITIONS THAT MUST MATCH ON BOTH SIDES, or the comparison is meaningless:
  run        = maximal consecutive same-GT block on one machine (plant: machine
               x GT x day, which is the same thing at day resolution)
  changeover = adjacent runs on one machine with different GT
  same-size  = that pair shares a rim (from gt_size)
  GT wait    = build end -> cure start, per tyre
  inventory  = time-average of the running (build - cure) balance
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
RUN = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/v19")
MONTH = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
FLOOR = CONFIG.thresholds.min_lot_units

# G4 -- REAL CHANGEOVER MINUTES, PER MACHINE, from the plant master
# `v_changeover_build`. NOT a flat pair.
#     PCR machines 1-5   28 same / 60 different
#     PCR machines 6-11  22 same / 42 different
#     TBR all            10 same / 24 different
# An earlier version of this script charged a flat 11.3/42.4 to BOTH plants.
# That understated PCR's same-size cost by ~2x and overstated TBR's
# different-size cost by 77 %, so every "weighted setup hours" figure it
# printed was wrong in magnitude -- and wrong by a DIFFERENT factor on each
# plant, so the two plants were not even comparable to each other.
SETUP: dict = {}
for _r in duck().execute(
        "SELECT machine, same_size_min, diff_size_min FROM v_changeover_build"
).fetchall():
    SETUP[_r[0]] = (float(_r[1]), float(_r[2]))
DEFAULT_SETUP = {"PCR": (22.0, 42.0), "TBR": (10.0, 24.0)}

sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
RIM = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
       if r.get("gt_code") and r.get("rim")}

rows: list[tuple] = []


def add(rule: str, metric: str, plant, ours, better: str = "") -> None:
    rows.append((rule, metric, plant, ours, better))


def fmt(v) -> str:
    if v is None:
        return "--"
    if isinstance(v, str):
        return v
    return f"{v:,.1f}" if isinstance(v, float) else f"{v:,}"


# --------------------------------------------------------------- PLANT SIDE
QB = """
SELECT plant, machineCode m, itemCode g, productionID pid, event_ts
FROM v_build
WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
  AND date >= ?::DATE AND date < ?::DATE
"""
# v_curing carries NO item code -- the GT comes from the barcode join
# (v_build.productionID = v_curing.gtbarCode, 99.6 % hit rate). Never invent
# another join key here; `b.gt_code IS NOT NULL` is cartesian and was a real bug.
QC = """
SELECT c.plant, c.wcID::VARCHAR press, b.itemCode g, c.gtbarCode pid, c.event_ts
FROM v_curing c
JOIN (SELECT productionID, any_value(itemCode) itemCode FROM v_build
      WHERE stage = 2 AND itemCode IS NOT NULL GROUP BY 1) b
  ON b.productionID = c.gtbarCode
WHERE c.date >= ?::DATE AND c.date < ?::DATE
"""
y, m = int(MONTH[:4]), int(MONTH[5:7])
lo = f"{y}-{m:02d}-01"
hi = f"{y + (m == 12)}-{(m % 12) + 1:02d}-01"

pb = pl.DataFrame(duck().execute(QB, [lo, hi]).fetchall(),
                  schema=["plant", "m", "g", "pid", "ts"], orient="row")
pc = pl.DataFrame(duck().execute(QC, [lo, hi]).fetchall(),
                  schema=["plant", "press", "g", "pid", "ts"], orient="row")

# ---------------------------------------------------------------- OUR SIDE
ob = pl.read_parquet(RUN / "build_schedule.parquet")
oc = pl.read_parquet(RUN / "cure_campaigns_reconciled.parquet")
ob_real = ob.filter(pl.col("machine") != "OPENING_STOCK")


def plant_runs(p: str) -> pl.DataFrame:
    """Plant runs: consecutive same-GT blocks per machine, ordered by time."""
    d = (pb.filter(pl.col("plant") == p).sort(["m", "ts"])
         .with_columns(pl.col("g").shift(1).over("m").alias("prev")))
    d = d.with_columns((pl.col("g") != pl.col("prev")).fill_null(True)
                       .cum_sum().over("m").alias("rid"))
    return (d.group_by(["m", "rid"]).agg(
        pl.col("g").first().alias("g"), pl.len().alias("q"),
        pl.col("ts").min().alias("t")).sort(["m", "t"]))


def our_runs(p: str) -> pl.DataFrame:
    return (ob_real.filter(pl.col("plant") == p)
            .group_by(["machine", "run_id"]).agg(
                pl.col("gt_code").first().alias("g"),
                pl.col("qty").sum().alias("q"),
                pl.col("start_ts").min().alias("t"))
            .rename({"machine": "m"}).sort(["m", "t"]))


def changeovers(r: pl.DataFrame, plant: str) -> tuple[int, int, float, int]:
    """(total, same-size, weighted hours, machine-days).

    Each changeover is charged at ITS OWN MACHINE's rate -- PCR machines differ
    (28/60 vs 22/42), so a plan that concentrates changeovers on the cheap
    machines genuinely costs less than one that spreads them.
    """
    d = r.with_columns(pl.col("g").shift(1).over("m").alias("prev"))
    ch = d.filter(pl.col("prev").is_not_null())
    same, mins = 0, 0.0
    for x in ch.iter_rows(named=True):
        s_min, d_min = SETUP.get(x["m"], DEFAULT_SETUP[plant])
        if RIM.get(x["g"]) == RIM.get(x["prev"]):
            same += 1
            mins += s_min
        else:
            mins += d_min
    wt = mins / 60.0
    md = r.with_columns(pl.col("t").dt.date().alias("d")).select(
        ["m", "d"]).unique().height
    return ch.height, same, wt, md


T0 = __import__("datetime").datetime(y, m, 1, 7, 0)
NH = ((__import__("datetime").datetime(y + (m == 12), (m % 12) + 1, 1)
       - __import__("datetime").datetime(y, m, 1)).days) * 24


def inv(build_ts, build_q, cure_ts, cure_q) -> tuple:
    """(time-weighted mean, time-weighted daily-mean max) of GT stock.

    WEIGHTED BY TIME, NOT BY EVENT. Inventory is a stock held over time, so the
    hours are the weights. Averaging the balance over EVENTS biases it upward,
    because events cluster where activity -- and therefore stock -- is high:
    measured +0.9 % on PCR but +5.7 % on TBR. Reporting the event-weighted figure
    made our TBR look 1,148 when the physical value is 1,086, and made the rail
    look breached on days it was not.
    """
    a = pl.DataFrame({"ts": build_ts, "d": [float(x) for x in build_q]})
    b = pl.DataFrame({"ts": cure_ts, "d": [-float(x) for x in cure_q]})
    e = pl.concat([a, b]).sort("ts").with_columns(pl.col("d").cum_sum().alias("b"))
    ts = np.array([(x - T0).total_seconds() / 3600.0 for x in e["ts"]], float)
    bal = np.array(e["b"], float)
    idx = np.searchsorted(ts, np.arange(NH) + 0.5, side="right") - 1
    h = np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0)
    nd = (NH // 24) * 24
    return float(h.mean()), float(h[:nd].reshape(-1, 24).mean(axis=1).max())


for P in ("PCR", "TBR"):
    add(f"== {P} ==", "", "", "", "")

    pr, orr = plant_runs(P), our_runs(P)
    pq, oq = np.array(pr["q"], float), np.array(orr["q"], float)
    pch, psame, pwt, pmd = changeovers(pr, P)
    och, osame, owt, omd = changeovers(orr, P)
    fl = float(FLOOR.get(P, 0))

    # ---- volume & fulfilment
    pbt = pb.filter(pl.col("plant") == P).height
    pct = pc.filter(pl.col("plant") == P).height
    obt = float(ob.filter(pl.col("plant") == P)["qty"].sum())
    oct_ = float(oc.filter(pl.col("plant") == P)["qty_fed"].sum())
    add("G7", "tyres built", pbt, int(obt))
    add("G7", "tyres cured", pct, int(oct_))
    add("G1", "build / cure ratio (overbuild)", pbt / max(pct, 1),
        obt / max(oct_, 1), "1.00 = no overbuild")

    # ---- B12 lot size
    add("B12", "run count", pr.height, orr.height, "lower")
    add("B12", "run size p50", float(np.percentile(pq, 50)),
        float(np.percentile(oq, 50)), "higher")
    add("B12", "run size p25 / p75",
        f"{np.percentile(pq,25):,.0f} / {np.percentile(pq,75):,.0f}",
        f"{np.percentile(oq,25):,.0f} / {np.percentile(oq,75):,.0f}", "higher")
    add("B12", f"runs below floor ({fl:.0f})",
        f"{100*(pq<fl).mean():.1f}%", f"{100*(oq<fl).mean():.1f}%", "<= plant")

    # ---- B3 / B4 / B6 changeovers
    add("B6", "changeovers", pch, och, "lower")
    add("B6", "changeovers / machine-day", pch / max(pmd, 1), och / max(omd, 1),
        "lower")
    add("B3", "same-size share", f"{100*psame/max(pch,1):.1f}%",
        f"{100*osame/max(och,1):.1f}%", "higher")
    add("G4", "weighted setup hours", pwt, owt, "lower")
    add("B4", "size changes / machine-day", (pch - psame) / max(pmd, 1),
        (och - osame) / max(omd, 1), "lower")

    # ---- B5 campaign length
    add("B5", "run length p50, h",
        float(np.percentile(pq, 50)) * (58 if P == "PCR" else 204) / 3600,
        float(np.percentile(oq, 50)) * (58 if P == "PCR" else 204) / 3600,
        "higher")

    # ---- B9 / B10 / P6 / P7 machine spread
    pmg = pr.group_by("g").agg(pl.col("m").n_unique().alias("n"))
    omg = orr.group_by("g").agg(pl.col("m").n_unique().alias("n"))
    add("B9", "machines per GT, mean", float(pmg["n"].mean()),
        float(omg["n"].mean()), "lower")
    add("P7", "GTs on exactly 1 machine",
        f"{100*float((pmg['n']==1).mean()):.1f}%",
        f"{100*float((omg['n']==1).mean()):.1f}%", "higher")

    # ---- B14 SKUs per machine
    add("B14", "GTs per machine, month",
        float(pr.group_by("m").agg(pl.col("g").n_unique().alias("n"))["n"].mean()),
        float(orr.group_by("m").agg(pl.col("g").n_unique().alias("n"))["n"].mean()),
        "lower")

    # ---- B7 / P4 daily stability
    pd_ = (pb.filter(pl.col("plant") == P)
           .with_columns(pl.col("ts").dt.date().alias("d"))
           .group_by("d").len()["len"])
    od_ = (ob.filter(pl.col("plant") == P)
           .with_columns(pl.col("end_ts").dt.date().alias("d"))
           .group_by("d").agg(pl.col("qty").sum())["qty"])
    add("B7", "daily build CV", float(pd_.std() / pd_.mean()),
        float(od_.std() / od_.mean()), "lower")

    # ---- P5 machines / P8 presses used
    add("P5", "machines used", pr["m"].n_unique(), orr["m"].n_unique())
    add("P8", "presses used", pc.filter(pl.col("plant") == P)["press"].n_unique(),
        oc.filter(pl.col("plant") == P)["press"].n_unique(), "lower")

    # ---- C4 cure changeovers
    pcr_ = (pc.filter(pl.col("plant") == P).sort(["press", "ts"])
            .with_columns(pl.col("g").shift(1).over("press").alias("pv")))
    pcc = pcr_.filter(pl.col("pv").is_not_null() &
                      (pl.col("g") != pl.col("pv"))).height
    pcd = (pc.filter(pl.col("plant") == P)
           .with_columns(pl.col("ts").dt.date().alias("d"))
           .select(["press", "d"]).unique().height)
    ocr = (oc.filter(pl.col("plant") == P).sort(["press", "start_ts"])
           .with_columns(pl.col("gt_code").shift(1).over("press").alias("pv")))
    occ = ocr.filter(pl.col("pv").is_not_null() &
                     (pl.col("gt_code") != pl.col("pv"))).height
    # DENOMINATOR MUST BE PRESS-DAYS *OCCUPIED*, ON BOTH SIDES.
    # The plant side counts every (press, day) on which that press cured, because
    # every cure event is its own row. Our side is CAMPAIGNS, so counting
    # (press, start_date) counts one day per campaign -- and our campaigns run
    # 10-12 days. That undercounted the denominator ~10x and reported 0.38-0.49
    # against the plant's 0.08, i.e. a 5x defect that does not exist. Measured
    # apples-to-apples we are at 0.04 on both plants, against the plant's
    # 0.08 (PCR) and 0.04 (TBR) -- BETTER on PCR, matched on TBR, and 98 mould
    # changes against the plant's 217 in absolute terms.
    _occ_days = set()
    for _x in oc.filter(pl.col("plant") == P).iter_rows(named=True):
        _d0, _d1 = _x["start_ts"].date(), _x["end_ts"].date()
        for _k in range((_d1 - _d0).days + 1):
            _occ_days.add((_x["press"], _d0 + timedelta(days=_k)))
    ocd = len(_occ_days)
    add("C4", "mould changes (count)", pcc, occ, "lower")
    add("C4", "mould changes / press-day occupied", pcc / max(pcd, 1),
        occ / max(ocd, 1), "lower")

    # ---- S2 / S3 / G8 inventory, S4 / E1 aging
    jb = (pb.filter(pl.col("plant") == P).select(["pid", "ts"])
          .join(pc.filter(pl.col("plant") == P).select(["pid", "ts"]),
                on="pid", how="inner", suffix="_c"))
    pw = np.array(((jb["ts_c"] - jb["ts"]).dt.total_seconds() / 3600.0), float)
    pw = pw[(pw > 0) & (pw < 24 * 40)]
    obp = ob.filter(pl.col("plant") == P)
    ow = np.array(obp["wait_h"], float)
    add("E1", "GT wait mean, h", float(pw.mean()), float(ow.mean()), "lower")
    add("S4", "GT wait p50 / p95, h",
        f"{np.percentile(pw,50):.1f} / {np.percentile(pw,95):.1f}",
        f"{np.percentile(ow,50):.1f} / {np.percentile(ow,95):.1f}", "lower")
    add("R5", "GT wait max, h", float(pw.max()), float(ow.max()), "<= 72")

    pi, pdm = inv(list(jb["ts"]), [1.0] * jb.height,
                  list(jb["ts_c"]), [1.0] * jb.height)
    scale = pbt / max(jb.height, 1)
    oi, odm = inv(list(obp["end_ts"]), list(obp["qty"]),
                  list(obp["cure_ts"]), list(obp["qty"]))
    band = (CONFIG.gt_wip_min.get(P), CONFIG.gt_wip_max.get(P)) if hasattr(
        CONFIG, "gt_wip_min") else (None, None)
    add("G8", "mean GT inventory (time-wt)", pi * scale, oi,
        f"band {band[0]:,}-{band[1]:,}" if band[0] else "")
    _rail = {"PCR": 4800, "TBR": 1400}.get(P)
    add("G8", "GT inventory daily-mean MAX", pdm * scale, odm,
        f"rail {_rail:,}" + ("" if odm <= _rail else "  ** OVER **"))

print("=" * 104)
print(f"RULEBOOK SCORECARD   PLANT vs {RUN.name}   --   {MONTH}")
print("=" * 104)
print(f"{'rule':<6}{'metric':<36}{'PLANT':>16}{'OURS':>16}   {'better'}")
print("-" * 104)
for r, met, p, o, b in rows:
    if not met:
        print(f"\n{r}")
        continue
    print(f"{r:<6}{met:<36}{fmt(p):>16}{fmt(o):>16}   {b}")
print("-" * 104)

rec = pl.read_parquet(RUN / "cure_campaigns_reconciled.parquet")
dem = pl.read_parquet(ROOT / "warehouse" / "derived" / f"l45_lots_{MONTH}.parquet")
tot = float(sum((sum(r["lot_sizes"]) if r["lot_sizes"] is not None and len(r["lot_sizes"])
                 else r["lot_qty"] * r["n_lots"]) for r in dem.iter_rows(named=True)))
print(f"{'G7':<6}{'DEMAND FULFILMENT':<36}{'100.0%':>16}"
      f"{100*float(rec['qty_fed'].sum())/tot:>15.1f}%")
print("=" * 104)
