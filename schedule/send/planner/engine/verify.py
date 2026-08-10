"""PHASE 8 -- independent verification, and the KPI ladder.

The verifier MUST NOT call planner internals. It re-derives everything from the
output parquets. If it shares code with the planner it shares the planner's
bugs, and "0 violations" stops meaning anything.

Two checks were MISSING from the old verifier and are the reason a run could
report `hard_violations: 0` while breaching two hard business rules:

    S4/E1  shelf life 72h        -- 6.9% of output was over it
    G8     inventory TREND       -- WIP climbed 4x and never came back

TREND TEST, NOT BAND TEST. The plant's own stock swings sd ~530/day on a level
of ~4,820 (range ~3,400-6,200), so a days-in-band criterion would fail the plant
itself. What must hold is E[dI] ~ 0.
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from planner.runs.logger import log

from planner.config import GT_SHELF_LIFE_H as SHELF_LIFE_H  # hardcoded 72 h
TREND_TOL = 100.0          # |mean dI| and |slope| per day
DAY_START_H = 7            # plant day boundary


def _pairs(ev: pl.DataFrame) -> pl.DataFrame:
    """FIFO-rank build events against cure events per (plant, GT)."""
    sup = (ev.filter(pl.col("source").is_in(["build", "opening"])
                     & (pl.col("qty_delta") > 0))
           .with_columns(pl.col("qty_delta").cast(pl.Int64))
           .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i"))
           .explode("_i").sort(["plant", "gt_code", "ts"]))
    sup = sup.with_columns(
        pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("rk")
    ).select(["plant", "gt_code", "rk", pl.col("ts").alias("b_ts")])
    cur = (ev.filter(pl.col("source") == "cure").sort(["plant", "gt_code", "ts"])
           .with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("rk"))
           .select(["plant", "gt_code", "rk", pl.col("ts").alias("c_ts")]))
    return cur.join(sup, on=["plant", "gt_code", "rk"], how="inner").with_columns(
        ((pl.col("c_ts") - pl.col("b_ts")).dt.total_seconds() / 3600).alias("age_h"))


def verify(out_dir: Path) -> dict:
    b = _rd(out_dir / "build_schedule.parquet")
    c = _rd(out_dir / "cure_schedule.parquet")
    ev = _rd(out_dir / "gt_events.parquet")
    hard: list[dict] = []
    soft: list[dict] = []

    # H3 machine exclusivity
    if b.height:
        s = b.sort(["machine", "start_ts"])
        s = s.with_columns(pl.col("end_ts").shift(1).over("machine").alias("pe"))
        ov = s.filter(pl.col("pe").is_not_null()
                      & (pl.col("start_ts") < pl.col("pe") - pl.duration(seconds=1)))
        for r in ov.head(50).iter_rows(named=True):
            hard.append({"rule": "H3_machine_overlap", "machine": r["machine"],
                         "lot": r.get("lot_id"), "at": str(r["start_ts"])})

    # H2 press exclusivity
    if c.height:
        s = c.sort(["press", "start_ts"])
        s = s.with_columns(pl.col("end_ts").shift(1).over("press").alias("pe"))
        ov = s.filter(pl.col("pe").is_not_null()
                      & (pl.col("start_ts") < pl.col("pe") - pl.duration(seconds=1)))
        if ov.height:
            hard.append({"rule": "H2_press_overlap", "n": ov.height,
                         "first": str(ov["start_ts"][0])})

    j = _pairs(ev) if ev.height else pl.DataFrame()
    # H6 causality
    if j.height:
        bad = j.filter(pl.col("age_h") < 0)
        if bad.height:
            hard.append({"rule": "H6_cure_before_build", "n": bad.height})
    # H1 shelf life  <-- was missing
    over = j.filter(pl.col("age_h") > SHELF_LIFE_H).height if j.height else 0
    if over:
        hard.append({"rule": "H1_shelf_life_72h", "tyres": int(over),
                     "pct": round(100.0 * over / j.height, 2)})

    # G8 inventory trend  <-- was missing
    trend: dict = {}
    if ev.height:
        gg = (ev.with_columns(
                (pl.col("ts") - pl.duration(hours=DAY_START_H)).dt.date().alias("d"))
              .group_by(["plant", "d"]).agg(pl.col("qty_delta").sum().alias("net"))
              .sort(["plant", "d"]))
        gg = gg.with_columns(pl.col("net").cum_sum().over("plant").alias("w"))
        for plant in sorted(gg["plant"].unique().to_list()):
            w = gg.filter(pl.col("plant") == plant)["w"].to_list()
            if len(w) < 4:
                continue
            n = len(w)
            mx, my = (n - 1) / 2.0, sum(w) / n
            slope = (sum((i - mx) * (v - my) for i, v in enumerate(w))
                     / (sum((i - mx) ** 2 for i in range(n)) or 1.0))
            mean_d = (w[-1] - w[0]) / max(1, n - 1)
            sd = (sum((v - my) ** 2 for v in w) / n) ** 0.5
            trend[plant] = {"mean": int(my), "end": int(w[-1]), "sd": int(sd),
                            "mean_delta": round(mean_d, 1), "slope": round(slope, 1),
                            "pass": abs(mean_d) < TREND_TOL and abs(slope) < TREND_TOL}
            if not trend[plant]["pass"]:
                soft.append({"rule": "G8_inventory_trend", "plant": plant,
                             "slope": round(slope, 1), "mean_delta": round(mean_d, 1)})

    rep = {"hard": hard, "soft": soft, "n_hard": len(hard), "trend": trend,
           "aging": ({"p50": round(float(j["age_h"].median()), 1),
                      "p95": round(float(j["age_h"].quantile(0.95)), 1),
                      "max": round(float(j["age_h"].max()), 1),
                      "over_72h": int(over),
                      "over_72h_pct": round(100.0 * over / j.height, 2)}
                     if j.height else {})}
    (out_dir / "violations.json").write_text(json.dumps(rep, indent=2, default=str))
    log.info("engine.verify", n_hard=len(hard), n_soft=len(soft),
             aging_p95=rep["aging"].get("p95"))
    return rep


def _rd(p: Path) -> pl.DataFrame:
    return pl.read_parquet(p) if p.exists() else pl.DataFrame()


def _runs(df: pl.DataFrame, key: str) -> int:
    """Maximal same-GT blocks on a machine/press -- the physical run count."""
    if df.height == 0:
        return 0
    s = df.sort([key, "start_ts"])
    pv = s.select(pl.col("gt_code").shift(1).over(key).alias("p"),
                  pl.col("gt_code"))
    return int((pv["p"].is_null() | (pv["p"] != pv["gt_code"])).sum())


def _demand_by_gt(req) -> dict:
    if getattr(req, "demand", None) is None or req.demand.height == 0:
        return {}
    g = (req.demand.group_by(["plant", "gt_code"])
         .agg(pl.col("qty").sum().alias("D")).sort(["plant", "gt_code"]))
    return {(r["plant"], r["gt_code"]): float(r["D"])
            for r in g.iter_rows(named=True)}


def _capped(df: pl.DataFrame, req, td: float, kind: str) -> float:
    """CAPPED PER GT: sum(min(delivered_g, demand_g)) / sum(demand_g).

    Uncapped, over-delivery on one GT silently offsets under-delivery on
    another and the ratio can exceed 100% -- 2026-01 reported 100.14%. Supply
    legitimately exceeds demand (scrap gross-up plus opening stock carried in),
    so the surplus is real and is carried to the next month; what is wrong is
    letting it mask a shortfall elsewhere. The capped figure runs 1.1-1.4 pp
    below the uncapped one every month.
    """
    D = _demand_by_gt(req)
    if not D or not td or df.height == 0:
        return 0.0
    if kind == "cure":
        g = df.group_by(["plant", "gt_code"]).agg(pl.len().alias("C"))
    else:
        g = df.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("C"))
    tot = sum(min(float(r["C"]), D.get((r["plant"], r["gt_code"]), 0.0))
              for r in g.iter_rows(named=True))
    return round(100.0 * tot / td, 2)


def _overproduction(df: pl.DataFrame, req) -> int:
    """Tyres cured above demand -- surplus, carried into the next month."""
    D = _demand_by_gt(req)
    if not D or df.height == 0:
        return 0
    g = df.group_by(["plant", "gt_code"]).agg(pl.len().alias("C"))
    return int(sum(max(0.0, float(r["C"]) - D.get((r["plant"], r["gt_code"]), 0.0))
                   for r in g.iter_rows(named=True)))


def kpis(out_dir: Path, req) -> dict:
    """PHASE 8b -- the KPI ladder, in strict objective order."""
    b = _rd(out_dir / "build_schedule.parquet")
    c = _rd(out_dir / "cure_schedule.parquet")
    v = json.loads((out_dir / "violations.json").read_text())
    td = json.loads((out_dir / "true_demand.json").read_text())["total"]
    built = float(b["qty"].sum()) if b.height else 0.0
    cured = float(c.height)

    def span(df, s="start_ts", e="end_ts"):
        return 0.0 if df.height == 0 else (
            df[e].max() - df[s].min()).total_seconds() / 3600.0

    # CHANGEOVERS ARE COUNTED ON PHYSICAL RUNS, NOT ON LOT BOUNDARIES.
    # Adjacent same-GT lots merge into one run and incur no setup, so counting
    # lot boundaries inflates the denominator and understates minutes per
    # changeover: 4,761 "lots" were 1,755 real runs, and setup came out at
    # 8.2 min/lot against a changeover master of 28-60 min. The plant figure we
    # compare against is run-based, so this was not like-for-like.
    co_b = 0
    if b.height:
        s = b.sort(["machine", "start_ts"])
        pv = s.select(pl.col("gt_code").shift(1).over("machine").alias("p"),
                      pl.col("gt_code"))
        co_b = int((pv["p"].is_not_null() & (pv["p"] != pv["gt_code"])).sum())
    co_c = 0
    if c.height:
        s = c.sort(["press", "start_ts"])
        pv = s.select(pl.col("gt_code").shift(1).over("press").alias("p"),
                      pl.col("gt_code"))
        co_c = int((pv["p"].is_not_null() & (pv["p"] != pv["gt_code"])).sum())

    row = {
        # rank 1 feasibility
        "hard_violations": v["n_hard"],
        # rank 2 fulfilment -- CURED, not built. A green tyre never cured is
        # not fulfilment; scoring on built reported 100% while 4.4% of the
        # month was never made.
        "true_demand": int(td),
        # CAPPED PER GT. Uncapped, over-delivery on one GT silently offsets
        # under-delivery on another and the ratio can exceed 100% -- 2026-01
        # reported 100.14%. Supply legitimately exceeds demand (scrap gross-up
        # plus opening stock carried in), so the surplus is real; what is wrong
        # is letting it mask a shortfall elsewhere. True fulfilment is
        # sum(min(cured_g, demand_g)) / sum(demand_g), which runs 1.1-1.4 pp
        # below the uncapped figure every month.
        "cure_fulfilment_pct": _capped(c, req, td, "cure"),
        "build_fulfilment_pct": _capped(b, req, td, "build"),
        "cure_fulfilment_uncapped_pct": (round(100.0 * cured / td, 2)
                                         if td else 0.0),
        "over_production_tyres": _overproduction(c, req),
        # rank 3 shelf life
        "aging_p50_h": v["aging"].get("p50"),
        "aging_p95_h": v["aging"].get("p95"),
        "aging_over_72h_pct": v["aging"].get("over_72h_pct"),
        # rank 4 inventory trend
        "inventory": v["trend"],
        # rank 5-6
        "curing_changeovers": co_c,
        "building_changeovers": co_b,
        # PHYSICAL RUNS, not lot rows. Adjacent same-GT lots merge into one run
        # and incur no setup, so a lot count inflates apparent activity: 4,761
        # lots were 1,755 real runs. The plant figures are run-based.
        "building_runs": _runs(b, "machine"),
        "curing_runs": _runs(c, "press"),
        "setup_min_per_run": (round(float(b["setup_s"].sum()) / 60.0
                                    / max(_runs(b, "machine"), 1), 1)
                              if b.height else 0.0),
        "build_span_h": round(span(b), 1),
        "cure_span_h": round(span(c), 1),
        "total_span_h": round(max(span(b), span(c)), 1),
        "horizon_h": req.horizon_days * 24,
    }
    (out_dir / "kpi.json").write_text(json.dumps(row, indent=2, default=str))
    return row
