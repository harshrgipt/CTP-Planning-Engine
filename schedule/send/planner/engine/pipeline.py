"""THE ENGINE. Phases 0-9, deterministic end to end.

    P0 contract  -> P1 masters -> P2 feasibility gate
    -> [ P3 campaigns -> P4 controller -> P5 building -> P6 grid ] x P7 converge
    -> P8 verify -> P9 emit

PHASE 7 is a FIXED-POINT, not an estimate. The planner credits a campaign day
with a full press-day; the grid delivers ~87% of it (setup, starve, idle).
Driven off booked capacity the controller drains its own projection while
reality accumulates. The grid is deterministic and runs in ~10s, so run it and
re-target on what it actually cured. Do NOT introduce a fitted yield factor --
it would be wrong next month, exactly like rho and f_book were.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.engine.campaigns import plan_campaigns
from planner.engine.assign import assign_machines
from planner.engine.contract import PlanRequest, digest, ordered
from planner.engine import diagnostics as diag
from planner.engine.controller import controller_converged, cover_law, run_controller
from planner.engine.resolve import DEFAULT_RATE, Masters, feasibility, resolve_masters
from planner.engine.rightshift import right_shift
from planner.plan.building import _machine_size_lock, plan_building
from planner.plan.curing import plan_curing
from planner.plan.ledger import GreenTireLedger
from planner.plan.lots import build_lots
from planner.plan.rulekb import load_rules
from planner.plan.timing_lookup import TimingLookup
from planner.runs.logger import log

MAX_PASSES = 3


def _opening_qty(req: PlanRequest) -> dict[tuple[str, str], int]:
    if req.opening.height == 0:
        return {}
    g = (req.opening.group_by(["plant", "gt_code"]).len()
         .sort(["plant", "gt_code"]))
    return {(r["plant"], r["gt_code"]): int(r["len"]) for r in g.iter_rows(named=True)}


def _seed_ledger(req: PlanRequest) -> GreenTireLedger:
    """Opening stock keeps its TRUE build timestamp -- never clamped to the
    horizon open. The grid only pools tyres built strictly before a shift opens,
    so clamping made carry-over ineligible in the very first shift and, with the
    day-0 campaign rule, left the whole of day 1 uncured."""
    led = GreenTireLedger()
    if req.opening.height:
        ev = req.opening.select([
            pl.col("built_ts").alias("ts"), "plant", "gt_code",
            pl.lit(1.0).alias("qty_delta"), pl.lit("opening").alias("source"),
            (pl.lit("opening_") + pl.col("gt_code")).alias("lot_id")])
        led.con.register("_open", ev.to_arrow())
        led.con.execute("INSERT INTO gt_events SELECT * FROM _open")
        led.con.unregister("_open")
    return led


def _targets_to_demand(build_of: dict[str, dict[str, list[int]]],
                       days: list[date]) -> pl.DataFrame:
    rows = []
    for plant in ordered(build_of):
        for g in ordered(build_of[plant]):
            for d, q in enumerate(build_of[plant][g]):
                if q > 0:
                    rows.append({"plant": plant, "gt_code": g,
                                 "due_date": days[d], "qty": float(q)})
    return (pl.DataFrame(rows).sort(["plant", "due_date", "gt_code"])
            if rows else pl.DataFrame(
                schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                        "due_date": pl.Date, "qty": pl.Float64}))


def _realised(cure_df: pl.DataFrame, plan_start: date, H: int) -> dict:
    out: dict[str, dict[str, list[float]]] = {}
    if cure_df.height == 0:
        return out
    cd = cure_df.with_columns(
        ((pl.col("start_ts").dt.date() - pl.lit(plan_start)).dt.total_days())
        .alias("_d"))
    agg = (cd.group_by(["plant", "gt_code", "_d"]).len()
             .sort(["plant", "gt_code", "_d"]))
    for r in agg.iter_rows(named=True):
        d = int(r["_d"])
        if 0 <= d < H:
            out.setdefault(r["plant"], {}).setdefault(
                r["gt_code"], [0.0] * H)[d] = float(r["len"])
    return out


def _ihat(led: pl.DataFrame, plan_start: date, H: int) -> dict:
    """TIME-INTEGRAL inventory per (plant, gt, day) -- the controller's observer.

    The controller balances build against cure at DAY granularity, so its state
    variable is a day ENDPOINT while the ledger is a continuous-time integral.
    A day that balances perfectly still carries the area under its intra-day
    profile, and our build centroid leads the cure centroid, so:

        I_ledger = I_controller + lambda x delta_centroid

    Measured on July: the controller projected 4,113 for PCR while the ledger
    realized 7,332 -- a 3,219-tyre blind spot, 6.0h of W, and LARGER than the
    2,677 that separates us from the band. Capping the projection did nothing
    because 4,113 was already compliant; the observable was wrong by 78%.

    Segments are attributed to the day their event opens. With ~1M events over
    31 days the mean segment is seconds long, and the denominator is covered
    time rather than a flat 24h, so a segment crossing midnight cannot distort
    the mean of either day.
    """
    if led.height == 0:
        return {}
    e = (led.with_columns(
            pl.when(pl.col("source") == "cure").then(-pl.col("qty_delta").abs())
            .otherwise(pl.col("qty_delta").abs()).alias("d"))
         .sort(["plant", "gt_code", "ts"])
         .with_columns(pl.col("d").cum_sum().over(["plant", "gt_code"]).alias("bal")))
    e = e.with_columns(
        pl.col("ts").shift(-1).over(["plant", "gt_code"]).alias("nxt"))
    e = e.with_columns(
        ((pl.col("nxt") - pl.col("ts")).dt.total_seconds() / 3600.0)
        .fill_null(0.0).clip(lower_bound=0.0).alias("dur"),
        ((pl.col("ts").dt.date() - pl.lit(plan_start)).dt.total_days()).alias("day"))
    agg = (e.filter((pl.col("day") >= 0) & (pl.col("day") < H) & (pl.col("dur") > 0))
           .group_by(["plant", "gt_code", "day"])
           .agg((pl.col("bal") * pl.col("dur")).sum().alias("area"),
                pl.col("dur").sum().alias("t"))
           .with_columns((pl.col("area") / pl.col("t")).alias("ihat"))
           .sort(["plant", "gt_code", "day"]))
    out: dict = {}
    for r in agg.iter_rows(named=True):
        out.setdefault(r["plant"], {}).setdefault(
            r["gt_code"], [0.0] * H)[int(r["day"])] = float(r["ihat"])
    return out


def _replay_build(ledger: GreenTireLedger, build_df: pl.DataFrame) -> None:
    """Re-emit build credits from a mutated build schedule.

    Mirrors `plan/building.py` exactly: one +1 per tyre at
    `setup_end + cycle_s x (i+1)`, bulk-inserted via Arrow. Deferring or
    trimming lots invalidates the credits the builder already wrote, and the
    ledger -- not the schedule frame -- is what the grid and the verifier read.
    """
    if build_df.height == 0:
        return
    t = (build_df
         .with_columns(pl.col("qty").cast(pl.Int64),
                       (pl.col("start_ts")
                        + pl.duration(seconds=pl.col("setup_s"))).alias("setup_end"))
         .with_columns(pl.int_ranges(pl.col("qty")).alias("_i")).explode("_i"))
    ev = t.select([
        (pl.col("setup_end") + pl.duration(
            seconds=pl.col("cycle_s") * (pl.col("_i").cast(pl.Float64) + 1.0))
         ).alias("ts"),
        pl.col("plant"), pl.col("gt_code"),
        pl.lit(1.0).alias("qty_delta"), pl.lit("build").alias("source"),
        pl.col("lot_id")])
    ledger.con.register("_rb", ev.to_arrow())
    ledger.con.execute("INSERT INTO gt_events SELECT * FROM _rb")
    ledger.con.unregister("_rb")


def run_engine(req: PlanRequest, kb_dir: Path, out_dir: Path) -> dict:
    """Execute all phases. Returns the run report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    days = [req.plan_start + timedelta(days=i) for i in range(req.horizon_days)]
    report: dict = {"input": req.to_dict()}

    # ---- P1 masters, P2 gate -------------------------------------------
    timing = TimingLookup(kb_dir / "learn")
    kbase = load_rules(kb_dir / "rules.duckdb")
    ms = resolve_masters(req, timing)
    report["masters"] = ms.to_dict()
    gate = feasibility(req, ms)
    report["feasibility"] = gate
    if not gate["go"]:
        (out_dir / "run_report.json").write_text(json.dumps(report, indent=2, default=str))
        log.error("engine.infeasible")
        return report

    opening_q = _opening_qty(req)
    tot = (req.demand.group_by(["plant", "gt_code"])
           .agg(pl.col("qty").sum().alias("N")).sort(["plant", "gt_code"]))
    N_by = {p: {r["gt_code"]: float(r["N"])
                for r in tot.filter(pl.col("plant") == p).iter_rows(named=True)}
            for p in ordered(tot["plant"].unique().to_list())}

    # ---- SKIP LOW-DEMAND GTs (rule B12 / R9) ----------------------------
    # Below the threshold the set-up and mould change cost more than the order.
    # The demand is NOT deleted from the denominator -- `true_demand` still
    # carries it, so fulfilment reports the shortfall honestly and the skipped
    # list says why. Hiding it in the denominator would flatter the KPI.
    skipped: list[dict] = []
    if os.environ.get("PLANNER_SKIP_LOW_DEMAND") != "0":
        for plant in ordered(N_by):
            th = float(CONFIG.thresholds.min_demand_units.get(plant, 0))
            if th <= 0:
                continue
            drop = [g for g in ordered(N_by[plant]) if N_by[plant][g] < th]
            for g in drop:
                skipped.append({"plant": plant, "gt_code": g,
                                "demand_qty": N_by[plant].pop(g),
                                "threshold": th,
                                "reason": "month demand below minimum campaign"})
    if skipped:
        log.info("engine.skipped_low_demand", n=len(skipped),
                 tyres=int(sum(r["demand_qty"] for r in skipped)))
    T_g_h = float(CONFIG.thresholds.replenish_interval_h)
    # cover_h = a * draw^b, fitted per plant from history (b is negative:
    # high-runners are replenished more often and carry LESS cover).
    # COVER LAW DISABLED -- correct in principle, premature in practice.
    # The plant's cover really does fall with draw (r = -0.65 PCR / -0.80 TBR),
    # and the fit is clean: cover = 173.1*draw^-0.470 (PCR) / 226.4*draw^-0.698
    # (TBR). But applying it made every metric worse: fulfilment 99.16 -> 98.74,
    # aging p95 36.2 -> 38.9h, PCR inventory slope 150 -> 203.
    #
    # Why: the law cuts a high-runner's buffer to ~6.7h because the PLANT can
    # run that thin -- its mean build->cure lag is 9h. OURS is ~22h (aging p50
    # 19.8h), so our high-runners genuinely need the larger buffer and removing
    # it starves them. Our inventory is high because tyres WAIT, not because we
    # overbuild: I = lambda x W, and W is the defect.
    #
    # Re-enable only AFTER build targets move to SHIFT granularity. Until then
    # this removes buffer we still need.
    laws = ({p: (0.0, 0.0) for p in ordered(N_by)}
            if os.environ.get("PLANNER_COVER_LAW") != "1"
            else {p: cover_law(p) for p in ordered(N_by)})
    # PHASE 5a: commit each GT to one machine (the plant's measured rule).
    size_lock = {p: _machine_size_lock(p, timing) for p in ordered(N_by)}
    assigned = assign_machines(req, ms, timing, size_lock)
    log.info('engine.cover_law', **{p: [round(x, 4) for x in v]
                                    for p, v in laws.items()})

    # ---- P3..P7 --------------------------------------------------------
    realised: dict = {}
    ihat: dict = {}
    actual: dict = {}
    # CAP ACCOUNTING BASIS -- EXOGENOUS, ALWAYS. Demand per GT per day, fixed
    # by the order book. Every basis that depends on the capped plan's own
    # output is a self-amplifying deadlock, measured twice:
    #   realized cure schedule as envelope -> 490,133 -> 79,329 -> 16,582 cured
    #   realized cure per GT as drawdown   -> fulfilment 25.3%, stock 932
    # Both collapse because trimming build shrinks cure, which shrinks the
    # drawdown, which trims build harder. Demand cannot move, so it cannot
    # spiral -- and `build - demand` is exactly "stock above requirement",
    # which is the quantity the cap is meant to bound.
    # TOTAL demand per (plant, GT) -- a CEILING on delivery, not a pace.
    # Building still carries the scrap gross-up, because tyres really are lost
    # in production; but a press must not keep curing a GT once the month's
    # order is filled, or the surplus is delivered rather than held. Measured on
    # July, this and the R1 opening-stock netting together take over-production
    # from 5,796 to near zero.
    # NB this is a TOTAL, not the cumulative-by-day pacing line tried earlier --
    # that one throttled consumption without flooring production and cost 80% of
    # curing. A ceiling can only stop surplus at the end; it cannot starve.
    quota: dict[tuple[str, str], list[float]] = {}
    for r in (req.demand.group_by(["plant", "gt_code"])
              .agg(pl.col("qty").sum().alias("D"))
              .sort(["plant", "gt_code"]).iter_rows(named=True)):
        quota[(r["plant"], r["gt_code"])] = [float(r["D"])] * req.horizon_days
    dem_of: dict[str, dict[str, list[float]]] = {}
    for r in req.demand.sort(["plant", "gt_code", "due_date"]).iter_rows(named=True):
        di = (r["due_date"] - req.plan_start).days
        if 0 <= di < req.horizon_days:
            dem_of.setdefault(r["plant"], {}).setdefault(
                r["gt_code"], [0.0] * req.horizon_days)[di] += float(r["qty"])
    prev_ctrl: dict | None = None
    passes: list[dict] = []
    build_df = cure_df = pl.DataFrame()
    campaigns: dict = {}
    margin: dict[str, float] = {}
    for p_no in range(1, MAX_PASSES + 1):
        campaigns, profile, camp_stats = plan_campaigns(req, ms, opening_q, margin)

        build_of: dict[str, dict[str, list[int]]] = {}
        ctrl_stats: dict = {}
        for plant in ordered(N_by):
            rate = ms.rate.get(plant, DEFAULT_RATE.get(plant, 100.0))
            gts = ordered(N_by[plant])
            cure_src = realised.get(plant) or profile.get(plant, {})
            area = {g: N_by[plant][g] / ms.gt_rate.get((plant, g), rate)
                    for g in gts}
            b, s = run_controller(
                gts, req.horizon_days,
                {g: cure_src.get(g, [0.0] * req.horizon_days) for g in gts},
                N_by[plant], area,
                {g: float(opening_q.get((plant, g), 0)) for g in gts},
                ms.scrap.get(plant, 0.0), ms.zero_area.get(plant, 0.0), T_g_h,
                law=laws.get(plant),
                # G8 band midpoint sets the level. PLANNER_GT_TARGET=0 disables,
                # for the same one-build A/B reason as PLANNER_RIGHTSHIFT.
                target_I=(0.0 if os.environ.get("PLANNER_GT_TARGET") == "0" else
                          float(os.environ.get("PLANNER_TGT_SCALE",1.0)) * 0.5 * (CONFIG.thresholds.gt_wip_min.get(plant, 0)
                                 + CONFIG.thresholds.gt_wip_max.get(plant, 0))),
                cap=(0.0 if os.environ.get("PLANNER_GT_CAP") == "0" else
                     float(CONFIG.thresholds.gt_wip_cap.get(plant, 0))),
                real_of=dem_of.get(plant),
                # OBSERVER: OFF by default (PLANNER_OBSERVER=1 to arm).
                # The observable really is wrong by 78% -- the ledger integral
                # is 7,332 against a 4,113 day-endpoint projection -- but
                # closing the QUANTITY loop on it is a category error:
                #     I = I_endpoint + lambda x delta_centroid
                # The second term is TIMING. The controller only sets quantity,
                # so it cuts build to chase phase lead it cannot move, and
                # starves. Measured at Kp=0.3: fulfilment 99.04 -> 93.34,
                # inventory 7,327 -> 7,694 (UP), slope 57 -> 189, and the
                # controller's own projected stock went to -5,266.
                # Arm this only AFTER ALAP/EDD has cut delta_centroid, when the
                # integral and the endpoint converge and the target is reachable.
                # Per-GT release floor: the plant minimum, never above what the
                # GT needs for the whole month (a 40-tyre GT must not be
                # rounded to 150 -- that would breach G1).
                floor_of=(None if os.environ.get("PLANNER_ACCUM") == "0" else
                          {g: min(float(CONFIG.thresholds.min_lot_units
                                        .get(plant, 0)), N_by[plant][g])
                           for g in gts}),
                consume_of=(None if os.environ.get("PLANNER_TRUE_STOCK") != "1"
                            else actual.get(plant)),
                ihat_of=(ihat.get(plant)
                         if os.environ.get("PLANNER_OBSERVER") == "1" else None))
            build_of[plant] = b
            ctrl_stats[plant] = s

        # ---- P5 building ------------------------------------------------
        target = _targets_to_demand(build_of, days)
        lots = build_lots(target)
        ledger = _seed_ledger(req)
        unplaced: list = []
        build_df = plan_building(lots, kbase, timing, ledger, start=req.start_ts,
                                 assigned=assigned,
                                 horizon_end=req.start_ts + timedelta(
                                     days=req.horizon_days),
                                 unplaced=unplaced)

        # ---- P6 curing --------------------------------------------------
        cure_df = plan_curing(ledger, timing, req.start_ts, out_dir,
                              build_df=build_df, campaigns=campaigns,
                              quota=(None if os.environ.get("PLANNER_DEMAND_CAP") == "0"
                                     else quota))

        passes.append({"pass": p_no, "campaigns": camp_stats,
                       "controller": ctrl_stats,
                       "built": int(build_df["qty"].sum()) if build_df.height else 0,
                       "cured": cure_df.height})
        log.info("engine.pass", n=p_no,
                 built=passes[-1]["built"], cured=passes[-1]["cured"],
                 slope={k: v.get("wip_slope") for k, v in ctrl_stats.items()})

        merged = {"wip_slope": sum(v.get("wip_slope", 0.0) for v in ctrl_stats.values())}
        if controller_converged(prev_ctrl, merged) or p_no == MAX_PASSES:
            break
        prev_ctrl = merged
        # FEED BACK POTENTIAL, NOT REALISED. Realised cure is depressed by
        # starvation, and starvation is caused by under-building -- so feeding it
        # back makes the controller read the symptom of its own shortfall as a
        # lower requirement and cut again. Measured: built 499,350 -> 481,587 ->
        # 477,159 over three passes, a downward spiral rather than a fixed point.
        # Potential (what a mounted press could have taken) does not depend on
        # the build plan, so the iteration is stable.
        ihat = _ihat(ledger.con.execute(
            "SELECT * FROM gt_events ORDER BY plant, gt_code, ts, source, lot_id"
        ).pl(), req.plan_start, req.horizon_days)
        # what the grid ACTUALLY cured -- the controller's state update basis,
        # kept separate from `realised` so the TARGET still uses potential.
        actual = _realised(cure_df, req.plan_start, req.horizon_days)
        realised = getattr(cure_df, "_potential", None) or _realised(
            cure_df, req.plan_start, req.horizon_days)
        # BOOKING MARGIN IS DISABLED, and stays disabled. Sizing the press plan
        # at 1/fill to recover the ~3.5% shortfall was tried and is WORSE:
        #     margin        1.000 -> 1.044 -> 1.132   (it compounds)
        #     aging p95     38.2h -> 86.6h            (breaks the 72h limit)
        #     over 72h      0.43% -> 6.88%
        #     inv slope       215 -> 403
        #     fulfilment    96.53% -> 96.68%          (+0.15%, worthless)
        # Mounting a press you cannot feed starves it instead of leaving it
        # unmounted -- the loss is relabelled, not removed. This is the THIRD
        # time this class of fix has failed (rho gross-up, f_book, and now
        # measured margin). The residual 3.5% is NOT a booking problem; press
        # capacity already exceeds demand for every single GT. Do not retry.

    report["passes"] = passes
    report["ledger"] = ledger.con.execute(
        "SELECT source, count(*) n, sum(qty_delta) q FROM gt_events GROUP BY 1"
    ).pl().sort("source").to_dicts()

    # ---- P7b RIGHT-SHIFT, LAST ------------------------------------------
    # Pure slack removal: same lots, same per-machine sequence, same machine.
    # Fulfilment and changeover count are unchanged BY CONSTRUCTION, so this
    # cannot trade one KPI for another -- it either removes lead time or does
    # nothing. It must run last because every earlier stage's placement is an
    # input to the backward pass. The ledger's build events move with their
    # lots; leaving them behind would desync the ledger from the schedule and
    # the verifier reads the LEDGER, so the desync would surface as a phantom.
    # PLANNER_RIGHTSHIFT=0 disables the pass. This exists so the A/B is run on
    # ONE build of the code -- comparing against an older run directory silently
    # attributes every intervening change to this pass.
    if build_df.height and cure_df.height and os.environ.get(
            "PLANNER_RIGHTSHIFT", "1") != "0":
        led = ledger.con.execute(
            "SELECT * FROM gt_events ORDER BY plant, gt_code, ts, source, lot_id").pl()
        build_df, rs_stats = right_shift(
            build_df, cure_df, led, req.start_ts,
            req.start_ts + timedelta(days=req.horizon_days))
        report["right_shift"] = rs_stats
        sh = {k: v for k, v in zip(build_df["lot_id"].to_list(),
                                   build_df["rs_shift_h"].to_list()) if v}
        if sh:
            mv = pl.DataFrame({"lot_id": ordered(sh),
                              "dh": [sh[k] for k in ordered(sh)]})
            ledger.con.register("_rs", mv.to_arrow())
            ledger.con.execute(
                "UPDATE gt_events SET ts = ts + to_seconds(_rs.dh * 3600) "
                "FROM _rs WHERE gt_events.lot_id = _rs.lot_id "
                "AND gt_events.source = 'build'")
            ledger.con.unregister("_rs")

    # persist plan artefacts
    if build_df.height:
        build_df.write_parquet(out_dir / "build_schedule.parquet", compression="zstd")
    if cure_df.height:
        cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")
    # DuckDB does not guarantee row order without ORDER BY, so `SELECT *` alone
    # made gt_events.parquet non-reproducible even with identical content.
    (ledger.con.execute(
        "SELECT * FROM gt_events ORDER BY plant, gt_code, ts, source, lot_id"
    ).pl().write_parquet(out_dir / "gt_events.parquet", compression="zstd"))
    (out_dir / "true_demand.json").write_text(json.dumps(
        {"total": req.demand_total(),
         "by_plant": {p: float(sum(N_by[p].values())) for p in ordered(N_by)}},
        indent=2))
    camp_rows = [{"plant": k[0], "gt_code": k[1], "press": p, "start_day": s,
                  "end_day": e}
                 for k in ordered(campaigns) for p, s, e in campaigns[k]]
    if camp_rows:
        pl.DataFrame(camp_rows).sort(
            ["plant", "gt_code", "start_day", "press"]).write_parquet(
            out_dir / "press_campaigns.parquet", compression="zstd")

    # ---- P8b EXCEPTIONS -- feasibility before performance ----------------
    # KPIs computed on an infeasible plan are decoration. These are written on
    # every run so a reviewer never has to take a count on trust: R_g < 1 means
    # the presses mounted on that GT out-throughput what building supplies and
    # starve BY CONSTRUCTION; shelf-life breaches appear as rows with the lot
    # and both timestamps; horizon breaches and machine overlaps list the lot.
    H_end = req.start_ts + timedelta(days=req.horizon_days)
    led_df = ledger.con.execute(
        "SELECT * FROM gt_events ORDER BY plant, gt_code, ts, source, lot_id").pl()
    camp_df = (pl.read_parquet(out_dir / "press_campaigns.parquet")
               if (out_dir / "press_campaigns.parquet").exists() else pl.DataFrame())
    exc = {}
    for name, df in [
            ("supply_ratio", diag.supply_ratio(build_df, cure_df, camp_df)),
            ("shelf_life", diag.shelf_life_rows(led_df, cure_df)),
            ("past_horizon", diag.horizon_breaches(build_df, H_end)),
            ("machine_overlap", diag.overlaps(build_df)),
            ("unplaced", pl.DataFrame(unplaced) if unplaced else pl.DataFrame()),
            ("skipped_low_demand",
             pl.DataFrame(skipped) if skipped else pl.DataFrame())]:
        if df.height:
            df.write_parquet(out_dir / f"exc_{name}.parquet", compression="zstd")
        exc[name] = int(df.height)
    if exc.get("supply_ratio"):
        sr = diag.supply_ratio(build_df, cure_df, camp_df)
        under = sr.filter(pl.col("excess_days") > 1.0)
        exc["supply_ratio_under_1"] = int(under.height)
        exc["supply_ratio_under_1_tyres"] = int(under["built"].sum()) if under.height else 0
    report["exceptions"] = exc
    log.info("engine.exceptions", **exc)

    report["run_id"] = digest(req.input_hash, ms.to_dict(),
                              CONFIG.thresholds.model_dump())
    (out_dir / "run_report.json").write_text(json.dumps(report, indent=2, default=str))
    return report
