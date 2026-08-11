"""ARM KPI -- one row of truth per (run, plant), recomputed from the plan parquets.

    PYTHONPATH=. python scripts/arm_kpi.py <run_dir> [<run_dir> ...] [--month 2026-07]
    PYTHONPATH=. python scripts/arm_kpi.py --json runs/a runs/b

Why this exists rather than reading `l11_invariants.parquet`: L11 grades against
its own gates, and this project has twice been burned by a scorecard belonging to
a different arm (PARTITION §4j.1). Everything here is re-derived from
`build_schedule.parquet` / `gt_events.parquet` in the directory named on the
command line, so a number cannot be inherited.

DEFINITIONS -- each one is a place this project has already been wrong.

  setup block   maximal consecutive same-GT block on ONE machine, SPLIT AT >1 h
                GAPS.  `run_id` alone understates the setup count by ~13 %
                because 8.8 % of groups contain a gap (PARTITION §4f).  Sorted
                by (plant, machine, start_ts) EXPLICITLY before any shift(),
                because a display sort doubles as the measurement (§1h).
  changeover    adjacent setup blocks on one machine with a different GT.
                count = blocks - machines, which is checked here.
  same-size     that adjacent pair shares a rim, from `gt_size`.
  weighted CO   charged at THAT MACHINE's own minutes from `v_changeover_build`
                (PCR m1-5 28/60, m6-11 22/42, TBR 10/24).  Never a flat pair (§1c).
  inventory     TIME-weighted mean of the running (build - cure) balance, sampled
                hourly.  Event-weighting biases +5.7 % on TBR (§1e).
  rim switch    adjacent setup blocks on one machine whose RIMS differ.  A rim
                STREAK is a maximal consecutive block-run of one rim; its length
                is counted in BLOCKS and in TYRES.
  occupancy     sum(block duration) / (machines x month hours).  Idle is the
                complement, and `holes` are the gaps BETWEEN blocks (a machine
                that finishes early leaves a tail, not a hole).

`OPENING_STOCK` rows are not builds -- they are opening inventory drawn at t0 --
so they are excluded from every build-side metric and kept only in fulfilment.
"""
from __future__ import annotations

import calendar
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
IN = paths.INPUT_DERIVED

_sz = pl.read_parquet(IN / "gt_size.parquet")
RIM = {r["gt_code"]: str(r["rim"]) for r in _sz.iter_rows(named=True)
       if r.get("gt_code") and r.get("rim")}

# SISTER ADJACENCY -- measured directly, because the COST MODEL CANNOT SHOW IT.
# `v_changeover_build` is keyed on (machine x same-size/different-size) only: it
# has no component or sister dimension, so a sister transition and a non-sister
# same-rim transition are charged the SAME 10 min on TBR. In the plant's own
# history those two cost 5.2 min and 12.1 min of realised gap. Any benefit from
# sister sequencing is therefore invisible in `weighted_setup_h` by construction
# and has to be read from this share instead.
SIS: dict[str, str] = {}
try:
    for _r in pl.read_parquet(IN / "gt_sister_group.parquet").iter_rows(named=True):
        SIS[_r["gt_code"]] = str(_r["sister_id"])
except Exception:
    pass

# CONSTRUCTION-CLUSTER ADJACENCY -- same argument as SIS above, and the same
# blindness in the cost model. Scored only where BOTH sides of a transition
# carry a cluster id: PCR workbook coverage is 68.5 % of volume, so counting an
# uncovered GT as "not same cluster" would report coverage as behaviour.
# Plant reference (Feb-Jul, blocks split at >1 h): TBR 67.2 %, PCR 14.1 %,
# against within-machine permutation nulls of 44.2 % and 10.0 %.
CLU: dict[tuple, str] = {}
try:
    for _r in pl.read_parquet(IN / "sku_con_cluster.parquet").iter_rows(named=True):
        if _r.get("con_cluster"):
            CLU[(_r["plant"], _r["gt_code"])] = str(_r["con_cluster"])
except Exception:
    pass

# Per-machine changeover minutes from the plant master. Never hardcode (§1c).
SETUP: dict[str, tuple[float, float]] = {}
try:
    from planner.data.warehouse import duck
    for _r in duck().execute(
            "SELECT machine, same_size_min, diff_size_min "
            "FROM v_changeover_build").fetchall():
        SETUP[_r[0]] = (float(_r[1]), float(_r[2]))
except Exception:
    pass
DEFAULT_SETUP = {"PCR": (22.0, 42.0), "TBR": (10.0, 24.0)}
FLOOR = {"PCR": 150.0, "TBR": 70.0}
RAIL = {"PCR": 4800.0, "TBR": 1400.0}


def _blocks(b: pl.DataFrame) -> pl.DataFrame:
    """True setup blocks: consecutive same-GT on one machine, split at >1 h gaps."""
    b = b.filter(pl.col("machine") != "OPENING_STOCK")
    if b.is_empty():
        return b
    # Deduplicate: build_schedule carries one row per (slice, press) delivery, so
    # the same physical slice appears once per press it feeds. A block is machine
    # time, and machine time must not be double counted.
    b = (b.group_by(["plant", "machine", "gt_code", "start_ts", "end_ts"])
           .agg(pl.col("qty").sum())
           .sort(["plant", "machine", "start_ts"]))          # EXPLICIT (§1h)
    b = b.with_columns([
        pl.col("gt_code").shift(1).over(["plant", "machine"]).alias("_pg"),
        pl.col("end_ts").shift(1).over(["plant", "machine"]).alias("_pe"),
    ])
    gap_h = ((pl.col("start_ts") - pl.col("_pe")).dt.total_seconds() / 3600.0)
    newblk = (pl.col("_pg").is_null() | (pl.col("gt_code") != pl.col("_pg"))
              | (gap_h > 1.0))
    b = b.with_columns(newblk.cast(pl.Int32).cum_sum()
                       .over(["plant", "machine"]).alias("blk"))
    return (b.group_by(["plant", "machine", "blk"])
             .agg([pl.col("gt_code").first(),
                   pl.col("start_ts").min().alias("st"),
                   pl.col("end_ts").max().alias("en"),
                   pl.col("qty").sum()])
             .sort(["plant", "machine", "st"]))


def _inv(ev: pl.DataFrame, plant: str) -> tuple[float, float]:
    """Time-weighted mean and max daily-mean of the GT balance. Hourly sample."""
    e = ev.filter(pl.col("plant") == plant).sort("ts")
    if e.is_empty():
        return 0.0, 0.0
    ts = e["ts"].to_numpy().astype("datetime64[s]").astype(np.int64)
    bal = np.cumsum(e["d"].to_numpy().astype(float))
    t0, t1 = ts[0], ts[-1]
    grid = np.arange(t0, t1 + 3600, 3600)
    idx = np.searchsorted(ts, grid, side="right") - 1
    idx = np.clip(idx, 0, len(bal) - 1)
    g = bal[idx]
    day = (grid - t0) // 86400
    dmax = max(float(g[day == d].mean()) for d in np.unique(day))
    return float(g.mean()), dmax


def kpi(run: Path, month: str) -> dict:
    b = pl.read_parquet(run / "build_schedule.parquet")
    ev = pl.read_parquet(run / "gt_events.parquet")
    yr, mo = int(month[:4]), int(month[5:7])
    month_h = calendar.monthrange(yr, mo)[1] * 24.0
    blk = _blocks(b)
    out: dict[str, dict] = {}
    for p in ["PCR", "TBR"]:
        bp = b.filter(pl.col("plant") == p)
        if bp.is_empty():
            continue
        kb = blk.filter(pl.col("plant") == p) if not blk.is_empty() else blk
        machines = sorted(kb["machine"].unique().to_list()) if not kb.is_empty() else []
        nm = len(machines)

        # --- changeovers, same-size, weighted setup, rim switches, streaks ---
        n_co = n_same = 0
        n_sis = n_sis_scored = 0        # sister adjacency, scored pairs only
        n_clu = n_clu_scored = 0        # construction-cluster adjacency
        # MEASURED INTER-RUN GAP, split by the cluster relation of the pair.
        # `v_changeover_build` is keyed on (machine x same/different SIZE) only,
        # so it charges a same-cluster and a different-cluster same-size
        # transition identically. Any cluster benefit is therefore invisible in
        # `weighted_setup_h` BY CONSTRUCTION and has to be read here instead:
        # the wall-clock hole our own plan leaves between two adjacent blocks.
        # Scored only where BOTH sides carry a cluster id (same rule as
        # `cluster_adj_pct`) and only on SAME-SIZE pairs, which is the
        # population the plant's own signal was measured on.
        gap_same_clu: list[float] = []
        gap_diff_clu: list[float] = []
        wmin = 0.0
        n_rimsw = 0
        streak_blocks: list[int] = []
        streak_qty: list[float] = []
        per_mach_sw: dict[str, int] = {}
        busy_s = 0.0
        holes: list[float] = []
        for m in machines:
            g = kb.filter(pl.col("machine") == m).sort("st")
            gts = g["gt_code"].to_list()
            qty = g["qty"].to_list()
            st = g["st"].to_list()
            en = g["en"].to_list()
            busy_s += sum((e - s).total_seconds() for s, e in zip(st, en))
            for i in range(1, len(st)):
                h = (st[i] - en[i - 1]).total_seconds() / 3600.0
                if h > 1e-9:
                    holes.append(h)
            same_min, diff_min = SETUP.get(m, DEFAULT_SETUP[p])
            cur_r, cur_n, cur_q = RIM.get(gts[0]), 1, qty[0]
            per_mach_sw[m] = 0
            for i in range(1, len(gts)):
                if gts[i] == gts[i - 1]:
                    # cannot happen after blocking except across a >1 h gap;
                    # that IS a setup, and it is a same-GT one.
                    n_co += 1
                    n_same += 1
                    wmin += same_min
                else:
                    n_co += 1
                    ra, rb = RIM.get(gts[i - 1]), RIM.get(gts[i])
                    if ra is not None and rb is not None and ra == rb:
                        n_same += 1
                        wmin += same_min
                    else:
                        wmin += diff_min
                # Sister adjacency is scored ONLY where BOTH sides carry a
                # signature -- a GT with no construction row is not evidence
                # either way, and counting it as "not a sister" would make
                # coverage look like behaviour. (Same class as the August
                # same-size figure, which reads 65.2 % only because 27.9 % of
                # its changeovers involve a GT with no rim.)
                if gts[i] in SIS and gts[i - 1] in SIS and gts[i] != gts[i - 1]:
                    n_sis_scored += 1
                    if SIS[gts[i]] == SIS[gts[i - 1]]:
                        n_sis += 1
                if ((p, gts[i]) in CLU and (p, gts[i - 1]) in CLU
                        and gts[i] != gts[i - 1]):
                    n_clu_scored += 1
                    _same_clu = CLU[(p, gts[i])] == CLU[(p, gts[i - 1])]
                    if _same_clu:
                        n_clu += 1
                    _ra, _rb = RIM.get(gts[i - 1]), RIM.get(gts[i])
                    if _ra is not None and _ra == _rb:
                        _g = (st[i] - en[i - 1]).total_seconds() / 3600.0
                        (gap_same_clu if _same_clu else gap_diff_clu).append(_g)
                rn = RIM.get(gts[i])
                if rn != cur_r:
                    n_rimsw += 1
                    per_mach_sw[m] += 1
                    streak_blocks.append(cur_n)
                    streak_qty.append(cur_q)
                    cur_r, cur_n, cur_q = rn, 1, qty[i]
                else:
                    cur_n += 1
                    cur_q += qty[i]
            streak_blocks.append(cur_n)
            streak_qty.append(cur_q)

        q = kb["qty"].to_numpy() if not kb.is_empty() else np.array([0.0])
        inv_mean, inv_dmax = _inv(ev, p)
        # FULFILMENT = IN-MONTH output / PLANNABLE DEMAND. The denominator is
        # `net_requirement.gross_build` (this month's requirement), NOT the
        # campaign frame's own `qty` -- L5 may fail to PLACE demand, and that
        # loss must land in fulfilment rather than vanish from the denominator.
        # Using `qty` reads 98.05 % where the true figure is 96.95 %.
        try:
            cc = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
            cc = cc.filter(pl.col("plant") == p)
            fed = float(cc["qty_fed_in_month"].sum())
            req = (pl.read_parquet(ROOT / "warehouse" / "derived"
                                   / f"net_requirement_{month}.parquet")
                     .filter(~pl.col("residual") & (pl.col("plant") == p)))
            dem = float(req["gross_build"].sum())
            ful = 100.0 * fed / dem if dem else 0.0
        except Exception:
            fed = dem = ful = float("nan")
        wt = bp.filter(pl.col("machine") != "OPENING_STOCK")["wait_h"].to_numpy()
        occ = 100.0 * busy_s / (nm * month_h * 3600.0) if nm else 0.0
        # occupancy spread across machines
        per_occ = []
        for m in machines:
            g = kb.filter(pl.col("machine") == m)
            per_occ.append(100.0 * sum(
                (e - s).total_seconds() for s, e in
                zip(g["st"].to_list(), g["en"].to_list())) / (month_h * 3600.0))
        out[p] = {
            "fulfilment_pct": round(ful, 2),
            "fed": int(fed), "demand": int(dem),
            "blocks": int(kb.height), "machines": nm,
            "changeovers": n_co,
            "co_check_blocks_minus_machines": int(kb.height) - nm,
            "same_size_pct": round(100.0 * n_same / n_co, 1) if n_co else 100.0,
            "sister_adj_pct": round(100.0 * n_sis / n_sis_scored, 1)
                              if n_sis_scored else 0.0,
            "sister_scored": n_sis_scored,
            "cluster_adj_pct": round(100.0 * n_clu / n_clu_scored, 1)
                               if n_clu_scored else 0.0,
            "cluster_scored": n_clu_scored,
            "weighted_setup_h": round(wmin / 60.0, 1),
            "co_per_machine_day": round(n_co / (nm * month_h / 24.0), 2) if nm else 0.0,
            "rim_switches": n_rimsw,
            "rim_switches_per_machine": round(n_rimsw / nm, 2) if nm else 0.0,
            "machines_with_0_switch": sum(1 for v in per_mach_sw.values() if v == 0),
            "rim_switch_top": sorted(per_mach_sw.items(), key=lambda x: -x[1])[:4],
            "streak_blocks_p50": float(np.median(streak_blocks)) if streak_blocks else 0.0,
            "streak_blocks_mean": round(float(np.mean(streak_blocks)), 2) if streak_blocks else 0.0,
            "streak_qty_p50": float(np.median(streak_qty)) if streak_qty else 0.0,
            "subfloor_pct": round(100.0 * float((q < FLOOR[p]).mean()), 2),
            "lot_p50": float(np.median(q)), "lot_min": float(q.min()),
            "occupancy_pct": round(occ, 1),
            "occ_min": round(min(per_occ), 1) if per_occ else 0.0,
            "occ_max": round(max(per_occ), 1) if per_occ else 0.0,
            "occ_cv": round(float(np.std(per_occ) / np.mean(per_occ)), 3) if per_occ else 0.0,
            "idle_h": round(nm * month_h - busy_s / 3600.0, 0),
            "holes": len(holes),
            "hole_p50_h": round(float(np.median(holes)), 2) if holes else 0.0,
            "hole_total_h": round(float(np.sum(holes)), 1) if holes else 0.0,
            "n_gap_same_clu": len(gap_same_clu),
            "n_gap_diff_clu": len(gap_diff_clu),
            "gap_same_clu_p50_min": round(float(np.median(gap_same_clu)) * 60.0, 1)
                                    if gap_same_clu else 0.0,
            "gap_diff_clu_p50_min": round(float(np.median(gap_diff_clu)) * 60.0, 1)
                                    if gap_diff_clu else 0.0,
            "gap_clu_total_h": round(float(np.sum(gap_same_clu)
                                           + np.sum(gap_diff_clu)), 1)
                               if (gap_same_clu or gap_diff_clu) else 0.0,
            "inv_time_wt_mean": round(inv_mean, 0),
            "inv_daily_mean_max": round(inv_dmax, 0),
            "rail": RAIL[p],
            "rail_ok": bool(inv_dmax <= RAIL[p]),
            "r5_max_h": round(float(wt.max()), 1) if len(wt) else 0.0,
            "wait_p95_h": round(float(np.percentile(wt, 95)), 1) if len(wt) else 0.0,
            "wait_p50_h": round(float(np.percentile(wt, 50)), 1) if len(wt) else 0.0,
        }
    # L11 pass count
    try:
        inv11 = pl.read_parquet(run / "l11_invariants.parquet")
        out["_l11"] = {"pass": int((inv11["status"] == "PASS").sum()),
                       "n": int(inv11.height),
                       "fails": inv11.filter(pl.col("status") != "PASS")
                                     ["invariant"].to_list()}
    except Exception:
        out["_l11"] = {}
    return out


def main() -> int:
    args = [a for a in sys.argv[1:]]
    month = "2026-07"
    as_json = False
    runs = []
    i = 0
    while i < len(args):
        if args[i] == "--month":
            month = args[i + 1]; i += 2
        elif args[i] == "--json":
            as_json = True; i += 1
        else:
            runs.append(args[i]); i += 1
    allout = {}
    for r in runs:
        p = Path(r)
        if not p.is_absolute():
            p = ROOT / r
        allout[p.name] = kpi(p, month)
    if as_json:
        print(json.dumps(allout, indent=1, default=str))
        return 0
    hdr = ["ful%", "same%", "clus%", "sist%", "rimSw", "0sw", "strk", "CO",
           "setup_h", "occ%", "idle_h", "sub%", "lotP50", "inv", "dmax",
           "R5", "w95"]
    for name, d in allout.items():
        print(f"\n== {name}  ({month}) ==   L11 {d.get('_l11',{}).get('pass','?')}"
              f"/{d.get('_l11',{}).get('n','?')}")
        print("      " + "".join(f"{h:>9}" for h in hdr))
        for p in ["PCR", "TBR"]:
            if p not in d:
                continue
            k = d[p]
            print(f"  {p} " + "".join(f"{v:>9}" for v in [
                k["fulfilment_pct"], k["same_size_pct"], k["cluster_adj_pct"],
                k["sister_adj_pct"], k["rim_switches"],
                k["machines_with_0_switch"],
                k["streak_blocks_p50"], k["changeovers"], k["weighted_setup_h"],
                k["occupancy_pct"], int(k["idle_h"]), k["subfloor_pct"],
                int(k["lot_p50"]), int(k["inv_time_wt_mean"]),
                int(k["inv_daily_mean_max"]), k["r5_max_h"], k["wait_p95_h"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
