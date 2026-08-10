"""Demand loader with pluggable modes.

- master:         read masters/demand.{parquet,csv} if present.
- actual_month:   use that month's actual production as the demand target
                  (fair replay/backtest). Only valid inside 8mo history.
- proxy_prev28:   use previous 28-day window (default for forward planning —
                  no future info).
"""
from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

import polars as pl

from planner.data.masters import load as load_master
from planner.data.warehouse import duck
from planner.runs.logger import log


class DemandMode(str, Enum):
    MASTER = "master"
    ACTUAL_MONTH = "actual_month"
    PROXY_PREV28 = "proxy_prev28"


def window_demand(demand: pl.DataFrame, plan_start: date, plan_end: date,
                  window_frac: float | None = None) -> pl.DataFrame:
    """Give each GT a short, staggered ACTIVE WINDOW instead of the whole month.

    Measured invariant across all 8 months: the plant builds and cures each GT
    on the SAME days, in matched quantities --

        corr(built, cured) per GT-day  = 0.951 PCR / 0.912 TBR
        both stages active on          = 88 % / 86 % of GT-days
        daily cured/built ratio p50    = 0.97 / 0.93
        a GT is BUILT on 13.9 days and CURED on 15.6 days per month

    i.e. each GT runs hard for ~45 % of the month and is dormant the rest, with
    windows staggered so ~22 of 50 GTs are active on any day.

    We were spreading every GT across the whole horizon, so presses were sized

        n_g = N_g * tau_g / H            H = 744h

    instead of over the active window

        n_g = N_g * tau_g / (D_g * 24)   D_g ~ 14 days = 336h

    -- the same press-hours, but delivered as a month-long trickle no press can
    ever finish. That is why our curing ran 24.4 days per GT against building's
    16.5, and why the tail always landed outside the month.

    Windows tile the horizon, so cure span = H by construction.
    """
    from planner.config import CONFIG
    if demand.height == 0:
        return demand
    frac = window_frac if window_frac is not None else CONFIG.thresholds.gt_window_frac
    days = [plan_start + timedelta(days=i)
            for i in range((plan_end - plan_start).days + 1)]
    n = len(days)
    D = max(1, int(round(n * frac)))
    tot = (demand.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("total"))
                 .sort(["plant", "total"], descending=[False, True]))
    rows = []
    for plant in tot["plant"].unique().to_list():
        sub = tot.filter(pl.col("plant") == plant)
        k = sub.height
        n_slots = max(1, n - D + 1)
        for i, r in enumerate(sub.iter_rows(named=True)):
            # Round-robin the offsets over volume-sorted GTs so each window
            # carries a mix of large and small. Assigning offsets in volume
            # order instead front-loads every high-runner into the first window:
            # daily build load spikes and the build span ran to 807h, past the
            # month, even though total volume was unchanged.
            off = (i % n_slots) if n > D else 0
            per = float(r["total"]) / D
            for d in days[off:off + D]:
                rows.append({"plant": plant, "gt_code": r["gt_code"],
                             "due_date": d, "qty": per})
    out = pl.DataFrame(rows).filter(pl.col("qty") > 0)
    log.info("demand.windowed", window_days=D, of_days=n,
             gts=tot.height, rows=out.height)
    return out


def level_demand(demand: pl.DataFrame, plan_start: date, plan_end: date) -> pl.DataFrame:
    """Spread each GT's horizon demand evenly across the days, at its own rate.

    `proxy_prev28` copies the PREVIOUS month's day-by-day pattern, so a GT the
    plant happened to build on 7 days arrives as 7 demand days. Building then
    delivers it in 7 lumps (359/day) while its presses want it continuously
    (130/day over 20 days) -- PCR press 33 cured one batch on day 1 then sat
    idle 519 HOURS with zero supply arriving, which is where the cure span goes.

    Levelling makes each GT's build rate equal its cure rate

        r_g = N_g / H

    which is the build side of the corridor: with B_g(t) linear and C_g(t)
    linear at the same slope, the gap between them is bounded by construction.
    It also matches the plant, which holds daily output CV at 0.116 and builds
    ~22 SKUs every day rather than batching them.
    """
    if demand.height == 0:
        return demand
    days = [plan_start + timedelta(days=i)
            for i in range((plan_end - plan_start).days + 1)]
    tot = demand.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("total"))
    n = len(days)
    out = (tot.join(pl.DataFrame({"due_date": days}), how="cross")
              .with_columns((pl.col("total") / n).alias("qty"))
              .filter(pl.col("qty") > 0)
              .select(["plant", "gt_code", "due_date", "qty"]))
    log.info("demand.levelled", gts=tot.height, days=n, rows=out.height)
    return out


def drop_below_min_demand(demand: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Rule B16: drop GTs whose horizon demand is below the plant minimum.

    PCR 300 / TBR 150 tyres. Below that the changeover cost (28-60 min building,
    3.5-7 h mould change) outweighs the output, so the plant does not run them.
    """
    from planner.config import CONFIG
    if demand.height == 0:
        return demand, {}
    th = {"PCR": CONFIG.thresholds.min_demand_pcr, "TBR": CONFIG.thresholds.min_demand_tbr}
    totals = demand.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("tot"))
    keep = totals.filter(
        pl.col("tot") >= pl.col("plant").replace_strict(th, default=0).cast(pl.Float64)
    ).select(["plant", "gt_code"])
    out = demand.join(keep, on=["plant", "gt_code"], how="inner")
    dropped = totals.join(keep, on=["plant", "gt_code"], how="anti")
    stats = {
        "gts_dropped": dropped.height,
        "qty_dropped": float(dropped["tot"].sum() or 0),
        "gts_kept": keep.height,
        "thresholds": th,
    }
    if dropped.height:
        log.info("demand.min_demand_filter", **stats)
    return out, stats


def cap_to_curing_capacity(
    demand: pl.DataFrame, plan_start: date, plan_end: date, *,
    load_target: float | None = None,
) -> tuple[pl.DataFrame, dict]:
    """Trim demand to what the presses can actually absorb.

    GT has a hard shelf life (`Thresholds.gt_shelf_life_h`), so building more
    than curing can consume inside that window does not create inventory -- it
    creates scrap. Excess is therefore reported as demand shortfall rather than
    quietly queued.

    `load_target` < 1.0 deliberately leaves headroom: press wait time diverges
    as utilisation approaches 100%, so planning *at* capacity guarantees the
    shelf life is breached no matter how good the sequencing is.
    """
    from planner.config import CONFIG
    if load_target is None:
        load_target = CONFIG.thresholds.curing_load_target
    if demand.height == 0:
        return demand, {}

    days = (plan_end - plan_start).days + 1
    con = duck()
    stats: dict[str, dict] = {}
    keep = []
    for plant in demand["plant"].unique().to_list():
        # Service time = span/tyres per press (sustained throughput). Must match
        # TimingLookup._load_cure_cadence exactly -- if this is more optimistic
        # than the press timeline, demand passes the capacity test and then
        # queues forever. The plant states ~13,500/day PCR and the data agrees
        # (147/press-day = 587s), so this is the real ceiling.
        rows = con.execute(
            """
            WITH d AS (
                SELECT wcID::VARCHAR AS press,
                       date_diff('second', min(event_ts), max(event_ts))::DOUBLE
                           / NULLIF(count(*) - 1, 0) AS s_per_tyre
                FROM v_curing WHERE plant = ? AND statuscritical = 'Normal'
                GROUP BY 1 HAVING count(*) > 10
            )
            SELECT count(*), median(s_per_tyre) FROM d
            """, [plant]).fetchone()
        n_press, cad = (rows[0] or 0), (rows[1] or 0)
        sub = demand.filter(pl.col("plant") == plant)
        want = float(sub["qty"].sum())
        if not n_press or not cad:
            keep.append(sub)
            continue
        cap = n_press * days * 86400.0 / cad * load_target
        if want <= cap:
            keep.append(sub)
            stats[plant] = {"demand": want, "capacity": round(cap), "trimmed": 0.0,
                            "load_pct": round(100 * want / cap * load_target, 1)}
            continue
        scale = cap / want
        # Must stay whole tyres: the ledger credits per unit and casts to
        # BIGINT, so a fractional qty desynchronises supply from cure and shows
        # up as hard violations.
        keep.append(sub.with_columns(
            (pl.col("qty") * scale).round(0).alias("qty")
        ).filter(pl.col("qty") >= 1))
        stats[plant] = {"demand": want, "capacity": round(cap),
                        "trimmed": round(want - cap),
                        "load_pct": round(100 * want / (cap / load_target), 1)}
        log.warning("demand.capped_to_curing", plant=plant, requested=round(want),
                    capacity=round(cap), trimmed=round(want - cap))
    return pl.concat(keep), stats


def load_demand(
    plan_start: date,
    plan_end: date,
    *,
    mode: DemandMode | str = DemandMode.PROXY_PREV28,
) -> pl.DataFrame:
    """Return columns [plant, gt_code, due_date, qty]."""
    if isinstance(mode, str):
        mode = DemandMode(mode)

    if mode == DemandMode.MASTER:
        m = load_master("demand")
        if m.height:
            f = m.filter((pl.col("due_date") >= plan_start) & (pl.col("due_date") <= plan_end))
            if f.height:
                log.info("demand.master", rows=f.height)
                return f
        log.warning("demand.master_missing_falling_back")
        mode = DemandMode.PROXY_PREV28

    con = duck()
    if mode == DemandMode.ACTUAL_MONTH:
        df = con.execute(
            """
            SELECT plant, itemCode AS gt_code, date AS due_date, count(*)::DOUBLE AS qty
            FROM v_build
            WHERE stage = 2 AND QualityStatus = '1'
              AND date BETWEEN ? AND ?
            GROUP BY 1,2,3
            """,
            [plan_start, plan_end],
        ).pl()
        log.info("demand.actual_month", start=str(plan_start), end=str(plan_end), rows=df.height)
        return df

    # PROXY_PREV28 (default)
    horizon_days = (plan_end - plan_start).days + 1
    hist_end = plan_start - timedelta(days=1)
    hist_start = hist_end - timedelta(days=horizon_days - 1)
    df = con.execute(
        """
        SELECT plant, itemCode AS gt_code,
               CAST(? AS DATE) + (date_diff('day', ?, date))::INTEGER AS due_date,
               count(*)::DOUBLE AS qty
        FROM v_build
        WHERE stage = 2 AND QualityStatus = '1'
          AND date BETWEEN ? AND ?
        GROUP BY 1,2,3
        """,
        [plan_start, hist_start, hist_start, hist_end],
    ).pl()
    log.info("demand.proxy_prev28", hist_start=str(hist_start), hist_end=str(hist_end), rows=df.height)
    return df
