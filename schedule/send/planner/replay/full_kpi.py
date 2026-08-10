"""Full KPI panel per month — primary, efficiency, constraint adherence.

Reads a replay run directory (runs/<id>/month=*/…) and emits a wide report.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from planner.data.warehouse import duck
from planner.validate.violations import verify


def _month_bounds(tag: str) -> tuple[date, date]:
    y, m = [int(x) for x in tag.split("-")]
    d0 = date(y, m, 1)
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return d0, nxt - timedelta(days=1)


def _load(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def _makespan_s(df: pl.DataFrame, s_col="start_ts", e_col="end_ts") -> float:
    if df.height == 0:
        return 0.0
    return (df[e_col].max() - df[s_col].min()).total_seconds()


def _machine_util_pct(df: pl.DataFrame, machine_col: str) -> float:
    if df.height == 0:
        return 0.0
    span = (df["end_ts"].max() - df["start_ts"].min()).total_seconds()
    if span <= 0:
        return 0.0
    occ = (df["end_ts"] - df["start_ts"]).dt.total_seconds().sum()
    n = df[machine_col].n_unique()
    return 100.0 * float(occ) / (span * n)


def _idle_hours(df: pl.DataFrame, machine_col: str) -> float:
    """Sum of inter-lot gaps per machine (positive gaps only)."""
    if df.height == 0:
        return 0.0
    df2 = df.sort([machine_col, "start_ts"]).with_columns(
        pl.col("end_ts").shift(1).over(machine_col).alias("prev_end")
    )
    gaps = (df2["start_ts"] - df2["prev_end"]).dt.total_seconds()
    return float(gaps.filter(gaps > 0).sum() or 0) / 3600.0


def _changeovers(df: pl.DataFrame, machine_col: str, key_col: str = "gt_code") -> int:
    if df.height == 0:
        return 0
    d = df.sort([machine_col, "start_ts"]).with_columns(
        pl.col(key_col).shift(1).over(machine_col).alias("prev")
    )
    return int((d["prev"].is_not_null() & (d["prev"] != d[key_col])).sum())


def _daily_stability_cv(df: pl.DataFrame) -> float:
    """CV of daily produced qty."""
    if df.height == 0 or "qty" not in df.columns:
        return 0.0
    daily = df.with_columns(pl.col("start_ts").cast(pl.Date).alias("d")).group_by("d").agg(
        pl.col("qty").sum().alias("q")
    )
    q = daily["q"].to_numpy()
    if q.size < 2 or q.mean() == 0:
        return 0.0
    return float(q.std() / q.mean())


def _sync_pct(build: pl.DataFrame, cure: pl.DataFrame, sync_stats: dict | None) -> float:
    """Fraction of demand that reached cure = 1 − shortfall/demand."""
    if build.height == 0:
        return 0.0
    demand = int(build["qty"].sum())
    shortfall = int((sync_stats or {}).get("demand_shortfall", 0))
    return 100.0 * (demand - shortfall) / max(demand, 1)


def _on_time_pct(build: pl.DataFrame) -> float:
    """% of lots ending on/before due_date."""
    if build.height == 0 or "due_date" not in build.columns:
        return 0.0
    ok = build.filter(pl.col("end_ts").cast(pl.Date) <= pl.col("due_date")).height
    return 100.0 * ok / build.height


def _size_lock_pct(build: pl.DataFrame, kb_map: dict[str, str]) -> float:
    """% of consecutive lots on same machine sharing the same size class."""
    if build.height == 0 or not kb_map:
        return 0.0
    d = build.sort(["machine", "start_ts"]).with_columns(
        pl.col("gt_code").replace(kb_map, default="?").alias("_size"),
        pl.col("gt_code").shift(1).over("machine").alias("_prev_gt"),
    )
    d = d.with_columns(pl.col("_prev_gt").replace(kb_map, default="?").alias("_prev_size"))
    same = d.filter(pl.col("_prev_gt").is_not_null()
                    & (pl.col("_size") == pl.col("_prev_size"))).height
    tot = d.filter(pl.col("_prev_gt").is_not_null()).height
    return 100.0 * same / max(tot, 1)


def _stickiness_pct(build: pl.DataFrame) -> float:
    """% of consecutive lots on same machine with same gt_code = machine-SKU stickiness."""
    if build.height == 0:
        return 0.0
    d = build.sort(["machine", "start_ts"]).with_columns(
        pl.col("gt_code").shift(1).over("machine").alias("_p")
    )
    ok = d.filter(pl.col("_p").is_not_null()
                  & (pl.col("_p") == pl.col("gt_code"))).height
    tot = d.filter(pl.col("_p").is_not_null()).height
    return 100.0 * ok / max(tot, 1)


def _machine_variety(build: pl.DataFrame) -> float:
    """avg distinct SKUs per machine per day."""
    if build.height == 0:
        return 0.0
    d = build.with_columns(pl.col("start_ts").cast(pl.Date).alias("d")).group_by(
        ["machine", "d"]
    ).agg(pl.col("gt_code").n_unique().alias("nsku"))
    return float(d["nsku"].mean() or 0)


def _size_map_pcr() -> dict[str, str]:
    """gt_code → 'size' string. From v_construction_pcr."""
    con = duck()
    try:
        r = con.execute("SELECT gt_code, size FROM v_construction_pcr").fetchall()
        return {gt: str(sz) for gt, sz in r if gt and sz}
    except Exception:
        return {}


def compute_month(month_dir: Path) -> dict:
    tag = month_dir.name.replace("month=", "")
    b = _load(month_dir / "build_schedule.parquet")
    c = _load(month_dir / "cure_schedule.parquet")
    kpi = json.loads((month_dir / "kpi.json").read_text()) if (month_dir / "kpi.json").exists() else {}
    cmp = json.loads((month_dir / "compare.json").read_text()) if (month_dir / "compare.json").exists() else {}
    sync = cmp.get("planner", {}).get("_sync", {})  # in case we ever stash sync

    # Reconstruct sync_stats from artefacts.
    short_path = month_dir / "demand_shortfall.parquet"
    shortfall = pl.read_parquet(short_path).height if short_path.exists() else 0
    starve_path = month_dir / "starvation_unresolved.parquet"
    starve = pl.read_parquet(starve_path).height if starve_path.exists() else 0

    # Verifier hard-rule count.
    v = verify(month_dir)
    hard_violations = len(v.hard)

    demand_qty = float(b["qty"].sum()) if b.height else 0.0
    sync_pct = 100.0 * (demand_qty - shortfall) / max(demand_qty, 1)

    # TRUE demand, before the inventory controller declined anything. Without
    # this, `demand_fulfillment_pct` scores the plan against its own reduced
    # target and reports 100% while ~4% of the month was never built -- the
    # controller's refusal to overbuild showed up as a perfect score.
    tdp = month_dir / "true_demand.json"
    true_demand = float(json.loads(tdp.read_text()).get("total", 0.0)) if tdp.exists() else 0.0
    cured_qty = float(c.height)
    # DEMAND IS FINISHED TYRES. Building a green tyre and never curing it is not
    # fulfilment, so the honest headline is cured/demand, not built/demand.
    cure_ff = 100.0 * cured_qty / true_demand if true_demand > 0 else 0.0
    build_ff = 100.0 * demand_qty / true_demand if true_demand > 0 else 0.0

    # Shelf-life breach (rules S4/E1) -- the verifier does NOT check this, so a
    # run could report `hard_violations: 0` while 7% of output was scrap.
    over72 = age_p50 = 0.0
    lg = month_dir / "gt_events.parquet"
    inv_end = inv_mean = inv_slope = 0.0
    if lg.exists():
        ev = pl.read_parquet(lg)
        sup = (ev.filter(pl.col("source").is_in(["build", "opening"])
                         & (pl.col("qty_delta") > 0))
               .with_columns(pl.col("qty_delta").cast(pl.Int64))
               .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i")).explode("_i")
               .sort(["plant", "gt_code", "ts"]))
        sup = sup.with_columns(
            pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("rk")
        ).select(["plant", "gt_code", "rk", pl.col("ts").alias("b_ts")])
        cur = (ev.filter(pl.col("source") == "cure").sort(["plant", "gt_code", "ts"])
               .with_columns(pl.int_range(pl.len()).over(["plant", "gt_code"]).alias("rk"))
               .select(["plant", "gt_code", "rk", pl.col("ts").alias("c_ts")]))
        j = cur.join(sup, on=["plant", "gt_code", "rk"], how="inner").with_columns(
            ((pl.col("c_ts") - pl.col("b_ts")).dt.total_seconds() / 3600).alias("age_h"))
        if j.height:
            over72 = 100.0 * j.filter(pl.col("age_h") > 72).height / j.height
            age_p50 = float(j["age_h"].median())
        # inventory TREND, not band: the plant's own stock swings sd~530/day on a
        # level of ~4,800, so a days-in-band test would fail the plant itself.
        gg = (ev.with_columns((pl.col("ts") - pl.duration(hours=7)).dt.date().alias("d"))
                .group_by("d").agg(pl.col("qty_delta").sum().alias("net")).sort("d"))
        w = gg.with_columns(pl.col("net").cum_sum().alias("w"))["w"].to_list()
        if len(w) > 3:
            n_ = len(w)
            mx_ = (n_ - 1) / 2.0
            my_ = sum(w) / n_
            inv_slope = (sum((i - mx_) * (v - my_) for i, v in enumerate(w))
                         / (sum((i - mx_) ** 2 for i in range(n_)) or 1.0))
            inv_end, inv_mean = w[-1], sum(w) / n_

    # Primary
    row = {
        "month": tag,
        "demand_qty": int(demand_qty),
        "true_demand_qty": int(true_demand),
        "build_fulfillment_pct": round(build_ff, 2),
        "cure_fulfillment_pct": round(cure_ff, 2),
        "aging_over_72h_pct": round(over72, 2),
        "aging_p50_hours": round(age_p50, 1),
        "gt_inv_end": int(inv_end),
        "gt_inv_mean": int(inv_mean),
        "gt_inv_slope_per_day": round(inv_slope, 1),
        "demand_fulfillment_pct": round(kpi.get("demand_fulfillment_rate", 0) * 100, 2),
        "sync_pct":               round(sync_pct, 2),
        "makespan_hours":         round(_makespan_s(b) / 3600.0, 1),
        # Curing span was never measured, so a plan whose presses ran weeks past
        # month end still reported a passing makespan. The plant does build AND
        # cure in exactly 744h; both must be reported.
        "cure_makespan_hours":    round(_makespan_s(c) / 3600.0, 1),
        "total_span_hours":       round(
            0.0 if (b.height == 0 and c.height == 0) else
            (max([x for x in (b["end_ts"].max() if b.height else None,
                              c["end_ts"].max() if c.height else None) if x is not None])
             - min([x for x in (b["start_ts"].min() if b.height else None,
                                c["start_ts"].min() if c.height else None) if x is not None])
             ).total_seconds() / 3600.0, 1),
        "machine_util_pct":       round(kpi.get("machine_util_pct", 0), 2),
        "press_util_pct":         round(_machine_util_pct(c, "press"), 2),
        "on_time_pct":            round(_on_time_pct(b), 2),

        # Efficiency
        "building_changeovers":   _changeovers(b, "machine"),
        "curing_changeovers":     _changeovers(c, "press"),
        "gt_aging_p95_hours":     round(kpi.get("curing_wait_p95_s", 0) / 3600.0, 1),
        "avg_wip":                round(kpi.get("avg_wip", 0), 1),
        "machine_idle_hours":     round(_idle_hours(b, "machine"), 1),
        "press_idle_hours":       round(_idle_hours(c, "press"), 1),
        "daily_production_cv":    round(_daily_stability_cv(b), 3),

        # Constraint adherence
        "hard_violations":        hard_violations,
        "machine_sku_stickiness_pct": round(_stickiness_pct(b), 2),
        "size_lock_pct":          round(_size_lock_pct(b, _size_map_pcr()), 2),
        "avg_skus_per_machine_day": round(_machine_variety(b), 2),
        "starvation_events":      starve,
        "demand_shortfall":       shortfall,

        # Baseline comparison
        "wins_vs_history":        cmp.get("wins", None),
        "actual_util_pct":        round(cmp.get("actual", {}).get("machine_util_pct", 0), 2),
        "actual_changeovers":     cmp.get("actual", {}).get("changeover_count", None),
        "actual_gt_aging_p95_hours": round(
            cmp.get("actual", {}).get("curing_wait_p95_s", 0) / 3600.0, 1),
    }
    return row


def compute_run(run_dir: Path) -> list[dict]:
    return [compute_month(m) for m in sorted(run_dir.glob("month=*"))]
