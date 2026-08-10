"""Leak-free walk-forward evaluation over the full history.

The stock `replay` harness loads the *latest* learn run, which was mined from
all 8 months -- so every month it "predicts" was already in its training set.
Its own docstring claims it trains on `date < M`; the code never did. This
driver implements what the docstring promised.

For each target month M:
  1. set the warehouse cutoff to M-start, so no row at/after M is visible to
     anything downstream (all ten miners inherit this at the view layer);
  2. mine rules from `date < M`;
  3. plan M with PROXY_PREV28 demand, itself derived only from `date < M`;
  4. lift the cutoff and score the plan against what actually happened in M.

Steps 1-3 are strictly out-of-sample. Step 4 is scoring only -- nothing
learned there feeds back into the plan.

Modes:
  oos      walk-forward, leak-free (the headline result)
  insample one learn over all history, then plan every month with it (the
           leaky protocol -- run only to measure the optimism gap)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.learn.rule_extract import run_learn
from planner.plan.building import plan_building
from planner.plan.curing import plan_curing
from planner.plan.demand import (DemandMode, cap_to_curing_capacity,
                                 drop_below_min_demand, level_demand, load_demand,
                                 window_demand)
from planner.plan.inv_sim import simulate
from planner.plan.ledger import GreenTireLedger
from planner.plan.lots import build_lots
from planner.plan.rulekb import load_rules
from planner.plan.window_plan import plan_windows
from planner.plan.sync import sync
from planner.plan.timing_lookup import TimingLookup
from planner.replay.compare import compare
from planner.replay.kpi import compute
from planner.runs.logger import log
from planner.runs.run_context import RunContext


def _bounds(y: int, m: int) -> tuple[date, date]:
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def _months() -> list[tuple[int, int]]:
    set_cutoff(None)
    rows = duck().execute(
        "SELECT DISTINCT date_trunc('month', event_ts) AS m FROM v_build "
        "WHERE event_ts IS NOT NULL ORDER BY 1"
    ).fetchall()
    return [(r[0].year, r[0].month) for r in rows if r[0] is not None]


def plan_month(y: int, m: int, learn_run: Path, out_dir: Path, *,
               cutoff: date | None) -> dict:
    """Plan one month. `cutoff` is the horizon the planner may see (None = all)."""
    month_start, month_end = _bounds(y, m)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    set_cutoff(cutoff)
    kb = load_rules(learn_run / "rules.duckdb")
    timing = TimingLookup(learn_run / "learn")

    # Out-of-sample must not peek at the month's real output, so demand is
    # projected from the trailing window. In-sample mode uses the month's own
    # actuals, which is exactly the leak we are measuring.
    mode = DemandMode.PROXY_PREV28 if cutoff is not None else DemandMode.ACTUAL_MONTH
    demand = load_demand(month_start, month_end, mode=mode)
    # PAIRED CHANGE: windowed demand + per-DAY press sizing (USE_DAILY).
    # Sizing presses over the window instead of the month is the other half --
    # sum_g N_g*tau/(D_g*24) = 180 presses if every GT were live at once, but
    # staggering keeps ~45% live, so 0.45*180 = 81 of 95 fit. Demand-only was
    # tried alone and was worse, because concentrating arrivals without
    # concentrating capacity just queues harder:
    #   cure span 1,097h -> 1,203h / 1,255h with demand-only.
    # PAIRED TEST RESULT: windowing + per-window (daily) press sizing is WORSE
    # than either half -- cure span 992h -> 2,409h, curing changeovers 267 ->
    # 28,058, press util 61% -> 25%. The daily allocator also reported 201
    # presses against 175 that exist, so it has a cross-plant leak the
    # month-level path does not. Falsification condition stated in advance was
    # met: allocation-side changes cannot recover rho.
    # Concentrating a GT into 14 days needs 2.2x the presses DURING the window
    # (n_g = N_g*tau/(D_g*24), not /H) -- the press allocator still sizes over
    # the whole month, so the concentrated flow just queues harder. The demand
    # half of the change cannot work without the press half.
    # NB: level_demand() was tried and is worse -- it splits each GT into 31 tiny
    # daily lots, so every machine cycles all 52 GTs each day: building
    # changeovers 1,260 -> 2,461 with NO span improvement (1,350 -> 1,365h).
    # Smoothing demand alone does not fix press starvation.
    # CURING FIRST. Windows are sized and staggered, presses are committed to
    # (GT, window) campaigns, and building is then handed a per-(GT, day) target
    # table. Previously building committed first and curing consumed whatever
    # appeared, so rho -- the chance a press has stock when mounted -- was set
    # downstream and could not be repaired.
    #
    # Rules 2 (min run filter) and 3 (demand cap 0.92) are DROPPED here: the
    # first hides SKUs without recovering press-time, and the M/M/1 argument
    # behind the second does not apply to a deterministic schedule (the plant
    # itself runs 0.86-0.945). Re-baselining against full demand also makes the
    # comparison against the plant honest.
    min_stats = {"note": "B16 filter dropped -- reporting only"}
    cap_stats = {"note": "demand cap dropped -- full demand planned"}
    press_of: dict[tuple[str, str], list[str]] = {}
    try:
        _am = CONFIG.paths.warehouse / "derived" / "allowed_press_matrix.parquet"
        if _am.exists():
            import polars as _pl
            for r in _pl.read_parquet(_am).iter_rows(named=True):
                press_of.setdefault((r["plant"], r["gt_code"]), []).append(r["press"])
    except Exception as e:  # noqa: BLE001
        log.warning("wf.press_matrix_failed", err=str(e))
    # TWO-PASS FIXED POINT. The controller needs to know what the presses will
    # ACTUALLY cure, not what they were booked for: the planner credits a
    # campaign day with a full press-day while the shift grid delivers ~87% of
    # it (setup, starve, idle). Driven off booked capacity the controller drains
    # its own projection (slope -71/day) while reality accumulates. The grid is
    # deterministic and runs in ~10s, so instead of estimating a yield -- which
    # would be a fitted constant, and wrong next month -- run it, read the
    # realised cure per (GT, day), and re-target on that.
    base_demand = demand
    # Persist the demand BEFORE the controller sees it, so fulfilment is scored
    # against what was actually asked for rather than against the plan's own
    # reduced target.
    (out_dir / "true_demand.json").write_text(json.dumps({
        "total": float(base_demand["qty"].sum()) if base_demand.height else 0.0,
        "by_plant": {r["plant"]: float(r["qty"]) for r in
                     base_demand.group_by("plant").agg(pl.col("qty").sum())
                     .iter_rows(named=True)} if base_demand.height else {},
    }, indent=2))
    realised = None
    campaigns = None
    for _pass in (1, 2):
        campaigns, demand, win_stats = plan_windows(
            base_demand, month_start, month_end, press_of, timing,
            realised=realised)
        lots = build_lots(demand)
        start_ts = datetime.combine(month_start, datetime.min.time())
        ledger = GreenTireLedger()
        ledger.load_opening_from_mes(start_ts)
        build_df = plan_building(lots, kb, timing, ledger, start=start_ts)
        cure_df = plan_curing(ledger, timing, start_ts, out_dir,
                              build_df=build_df, campaigns=campaigns)
        if _pass == 2 or cure_df.height == 0:
            break
        H = (month_end - month_start).days + 1
        realised = {}
        cd = cure_df.with_columns(
            ((pl.col("start_ts").dt.date() - pl.lit(month_start)).dt.total_days())
            .alias("_d"))
        for r in (cd.group_by(["plant", "gt_code", "_d"]).len()
                    .iter_rows(named=True)):
            d = int(r["_d"])
            if 0 <= d < H:
                realised.setdefault(r["plant"], {}).setdefault(
                    r["gt_code"], [0.0] * H)[d] = float(r["len"])
        log.info("wf.pass1.realised", plants={k: len(v) for k, v in realised.items()},
                 cured=cure_df.height)
    sync_stats = sync(build_df, cure_df, ledger, out_dir)
    inv_stats = simulate(build_df, cure_df, ledger, out_dir)

    if build_df.height:
        build_df.write_parquet(out_dir / "build_schedule.parquet", compression="zstd")
    if cure_df.height:
        cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")

    kpi = compute(demand, build_df, cure_df, ledger)
    (out_dir / "kpi.json").write_text(json.dumps(kpi.to_dict(), default=str, indent=2))

    # Scoring needs the actual month, so the horizon is lifted here and here only.
    set_cutoff(None)
    try:
        cmp = compare(month_start, kpi)
        (out_dir / "compare.json").write_text(json.dumps(cmp.to_dict(), default=str, indent=2))
        wins = cmp.wins
    except Exception as e:  # noqa: BLE001
        log.error("wf.compare.failed", month=f"{y}-{m:02d}", err=str(e))
        wins = None

    (out_dir / "provenance.json").write_text(json.dumps({
        "month": f"{y}-{m:02d}",
        "cutoff": str(cutoff),
        "demand_mode": mode.value,
        "learn_run": learn_run.name,
        "out_of_sample": cutoff is not None,
        "curing_capacity": cap_stats, "min_demand_filter": min_stats,
        "windows": win_stats,
        "seconds": round(time.time() - t0, 1),
    }, indent=2))

    log.info("wf.month.done", month=f"{y}-{m:02d}", oos=cutoff is not None,
             wins=wins, lots=len(lots), sync=sync_stats,
             secs=round(time.time() - t0, 1))
    return {"month": f"{y}-{m:02d}", "wins": wins, "kpi": kpi.to_dict(),
            "sync": sync_stats, "inv": inv_stats,
            "seconds": round(time.time() - t0, 1)}


def run_oos(min_history_months: int = 1) -> Path:
    """Walk forward: re-learn from scratch before each month, then plan it."""
    months = _months()
    rc = RunContext.new(tag="walkforward-oos")
    log.info("wf.start", mode="oos", months=len(months), run_id=rc.run_id)

    summary = []
    for i, (y, m) in enumerate(months):
        if i < min_history_months:
            log.info("wf.skip.warmup", month=f"{y}-{m:02d}", prior_months=i)
            summary.append({"month": f"{y}-{m:02d}", "skipped": "insufficient_history"})
            continue
        cutoff = date(y, m, 1)
        try:
            t0 = time.time()
            learn_run = run_learn(cutoff=cutoff).parent   # returns rules.duckdb path
            learn_s = round(time.time() - t0, 1)
            row = plan_month(y, m, learn_run, rc.dir / f"month={y}-{m:02d}", cutoff=cutoff)
            row["learn_seconds"] = learn_s
            row["learn_run"] = learn_run.name
            summary.append(row)
        except Exception as e:  # noqa: BLE001
            log.error("wf.month.fail", month=f"{y}-{m:02d}", err=str(e))
            summary.append({"month": f"{y}-{m:02d}", "error": str(e)})
        (rc.dir / "summary.json").write_text(json.dumps(summary, default=str, indent=2))

    log.info("wf.done", mode="oos", run_id=rc.run_id)
    return rc.dir


def run_insample(learn_run: Path | None = None) -> Path:
    """Leaky baseline: one KB mined from everything, used to plan every month."""
    set_cutoff(None)
    if learn_run is None:
        # "-learn-" but not "-learn-asof" â€” the latter are the walk-forward KBs.
        cands = sorted(p for p in CONFIG.paths.runs.iterdir()
                       if p.is_dir() and "-learn-" in p.name and "-learn-asof" not in p.name)
        if not cands:
            raise FileNotFoundError("no full-history learn run; run `cli learn` first")
        learn_run = cands[-1]

    months = _months()
    rc = RunContext.new(tag="walkforward-insample")
    log.info("wf.start", mode="insample", months=len(months),
             learn_run=learn_run.name, run_id=rc.run_id)

    summary = []
    for (y, m) in months:
        try:
            row = plan_month(y, m, learn_run, rc.dir / f"month={y}-{m:02d}", cutoff=None)
            row["learn_run"] = learn_run.name
            summary.append(row)
        except Exception as e:  # noqa: BLE001
            log.error("wf.month.fail", month=f"{y}-{m:02d}", err=str(e))
            summary.append({"month": f"{y}-{m:02d}", "error": str(e)})
        (rc.dir / "summary.json").write_text(json.dumps(summary, default=str, indent=2))

    log.info("wf.done", mode="insample", run_id=rc.run_id)
    return rc.dir


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "oos"
    out = run_oos() if mode == "oos" else run_insample()
    print(f"RESULT_DIR={out}")








