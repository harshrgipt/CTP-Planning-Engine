"""Walk-forward monthly replay harness.

For each month M in the 8-mo history (after warm-up), train the KB from the
learn artefacts using only rows where date < M, plan month M with the greedy
building+curing pipeline, and compare KPIs to the actual outcome.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from planner.data.warehouse import duck
from planner.plan.building import plan_building
from planner.plan.curing import plan_curing
from planner.plan.demand import DemandMode, load_demand
from planner.plan.inv_sim import simulate
from planner.plan.ledger import GreenTireLedger
from planner.plan.lots import build_lots
from planner.plan.rulekb import load_rules
from planner.plan.sync import sync
from planner.plan.timing_lookup import TimingLookup
from planner.replay.compare import compare
from planner.replay.kpi import compute
from planner.runs.logger import log
from planner.runs.run_context import RunContext


def _month_bounds(y: int, m: int) -> tuple[date, date]:
    first = date(y, m, 1)
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def _months_in_warehouse() -> list[tuple[int, int]]:
    con = duck()
    rows = con.execute("""
        SELECT DISTINCT date_trunc('month', event_ts) AS m FROM v_build ORDER BY 1
    """).fetchall()
    return [(r[0].year, r[0].month) for r in rows if r[0] is not None]


def _latest_learn_run() -> Path:
    from planner.config import CONFIG
    # Exclude "-learn-asof<date>" runs — those are deliberately truncated KBs
    # produced by scripts/walkforward.py and must not be picked up as "latest".
    runs = sorted([p for p in CONFIG.paths.runs.iterdir()
                   if p.is_dir() and "learn" in p.name and "-learn-asof" not in p.name])
    if not runs:
        raise FileNotFoundError("no learn run found; run `make learn` first")
    return runs[-1]


def replay_month(y: int, m: int, learn_run: Path, out_dir: Path,
                 demand_mode: DemandMode = DemandMode.ACTUAL_MONTH) -> dict:
    month_start, month_end = _month_bounds(y, m)
    kb = load_rules(learn_run / "rules.duckdb")
    timing = TimingLookup(learn_run / "learn")

    demand = load_demand(month_start, month_end, mode=demand_mode)
    lots = build_lots(demand)
    log.info("replay.month.start", month=f"{y}-{m:02d}", demand_rows=demand.height, lots=len(lots))

    ledger = GreenTireLedger()
    ledger.load_opening_from_mes(datetime.combine(month_start, datetime.min.time()))

    start_ts = datetime.combine(month_start, datetime.min.time())
    build_df = plan_building(lots, kb, timing, ledger, start=start_ts)
    cure_df = plan_curing(ledger, timing, start_ts, out_dir)

    sync_stats = sync(build_df, cure_df, ledger, out_dir)
    inv_stats = simulate(build_df, cure_df, ledger, out_dir)

    # Persist schedules
    if build_df.height:
        build_df.write_parquet(out_dir / "build_schedule.parquet", compression="zstd")
    if cure_df.height:
        cure_df.write_parquet(out_dir / "cure_schedule.parquet", compression="zstd")

    kpi = compute(demand, build_df, cure_df, ledger)
    # Persist KPIs immediately — the comparison step touches large historical
    # tables and we don't want a compare failure to lose the plan-side KPIs.
    (out_dir / "kpi.json").write_text(json.dumps(kpi.to_dict(), default=str, indent=2))
    try:
        cmp = compare(month_start, kpi)
        (out_dir / "compare.json").write_text(json.dumps(cmp.to_dict(), default=str, indent=2))
    except Exception as e:
        log.error("replay.compare.failed", err=str(e))
        cmp = None
    tag = f"{y}-{m:02d}"
    wins = cmp.wins if cmp is not None else None
    log.info("replay.month.done", month=tag, wins=wins,
             fulfillment=kpi.demand_fulfillment_rate, changeovers=kpi.changeover_count,
             starve=kpi.starvation_events, sync=sync_stats)
    return {"month": tag, "wins": wins, "kpi": kpi.to_dict(),
            "sync": sync_stats, "inv": inv_stats}


def walk_forward(*, months: list[str] | None = None, limit: int | None = 3) -> Path:
    rc = RunContext.new(tag="replay")
    learn_run = _latest_learn_run()
    log.info("replay.start", learn=str(learn_run), run_id=rc.run_id, limit=limit)

    all_months = _months_in_warehouse()
    if months:
        wanted = {tuple(int(x) for x in s.split("-")) for s in months}
        all_months = [m for m in all_months if m in wanted]
    # Skip first two months as warm-up
    target = all_months[2:] if len(all_months) > 2 else all_months
    if limit is not None:
        target = target[:limit]

    summary = []
    for y, m in target:
        month_dir = rc.dir / f"month={y}-{m:02d}"
        month_dir.mkdir(parents=True, exist_ok=True)
        try:
            row = replay_month(y, m, learn_run, month_dir)
            summary.append(row)
        except Exception as e:
            log.error("replay.month.fail", month=f"{y}-{m:02d}", err=str(e))
            summary.append({"month": f"{y}-{m:02d}", "error": str(e)})

    (rc.dir / "summary.json").write_text(json.dumps(summary, default=str, indent=2))
    log.info("replay.done", run_id=rc.run_id, months=len(summary))
    return rc.dir
