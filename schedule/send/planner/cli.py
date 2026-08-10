"""Typer CLI. Entry: `python -m planner.cli <cmd>` inside the .venv."""
from __future__ import annotations

import typer

from planner.runs.logger import log

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def ingest(limit_rows: int = typer.Option(None, help="Optional row cap per CSV (smoke tests)")):
    """Stream all MES CSVs into the Parquet warehouse (Hive partitioned)."""
    from planner.data.ingest import ingest_all
    counts = ingest_all(limit_rows=limit_rows)
    log.info("cli.ingest.done", counts=counts)


@app.command()
def bom():
    """Normalize BOM xlsx into edges + SKU->GT map."""
    from planner.data.bom import run
    run()


@app.command()
def construction():
    """Ingest SKU construction mapping xlsx (PCR + TBR)."""
    from planner.data.construction import run
    run()


@app.command()
def balance():
    """Ingest TBR uniformity/balance sheets."""
    from planner.data.balance import run
    run()


@app.command()
def wip(as_of: str = typer.Option(..., help="YYYY-MM-DD planning cutoff")):
    """Derive opening WIP + GT inventory from MES history up to cutoff."""
    from planner.data.warehouse import duck
    con = duck()
    log.info("cli.wip.derive", as_of=as_of)
    # Placeholder query; full implementation in Phase 5.
    if not any(con.execute("SHOW TABLES").fetchall()):
        log.warning("cli.wip.no_warehouse")
        return
    rows = con.execute(
        f"""
        WITH gt_built AS (
            SELECT itemCode AS sku, count(*) AS built
            FROM v_build
            WHERE event_ts < TIMESTAMP '{as_of} 00:00:00'
            GROUP BY 1
        ),
        gt_cured AS (
            SELECT recipeID AS recipe, count(*) AS cured
            FROM v_curing
            WHERE event_ts < TIMESTAMP '{as_of} 00:00:00'
            GROUP BY 1
        )
        SELECT (SELECT sum(built) FROM gt_built) AS built_total,
               (SELECT sum(cured) FROM gt_cured) AS cured_total
        """
    ).fetchone()
    log.info("cli.wip.summary", built_total=rows[0], cured_total=rows[1])


@app.command()
def smoke():
    """1-day fixture-scale end-to-end: ingest a slice, build masters, verify."""
    from planner.data.ingest import ingest_all
    from planner.data.bom import run as bom_run
    from planner.data.construction import run as cons_run
    from planner.data.balance import run as bal_run
    from planner.data.warehouse import duck, refresh_views

    log.info("cli.smoke.start")
    counts = ingest_all(limit_rows=5_000)   # smoke: 5k rows per CSV
    bom_run()
    cons_run()
    bal_run()
    refresh_views()
    con = duck()
    for view in ("v_curing", "v_build", "v_consume", "v_bom", "v_bom_gt",
                 "v_construction_pcr", "v_construction_tbr",
                 "v_gt_template", "v_test_skus", "v_balance"):
        try:
            n = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
            log.info("cli.smoke.view", view=view, rows=n)
        except Exception as e:  # noqa: BLE001
            log.warning("cli.smoke.view_missing", view=view, err=str(e))
    log.info("cli.smoke.done", counts=counts)


@app.command()
def learn():
    """Phase 1+2: mine plant behaviour from warehouse and persist rules.duckdb."""
    from planner.learn.rule_extract import run_learn
    run_learn()


@app.command()
def replay(months: str = typer.Option(None, help="Comma-separated YYYY-M list, e.g. 2026-02,2026-03."),
           limit: int = typer.Option(3, help="Max # of months (default 3).")):
    """Walk-forward monthly replay: plan each historical month, compare KPIs."""
    from planner.replay.harness import walk_forward
    m_list = None
    if months:
        m_list = [s.strip() for s in months.split(",") if s.strip()]
    walk_forward(months=m_list, limit=limit)


@app.command()
def plan(month: str = typer.Option(..., help="YYYY-MM to plan (uses latest KB)"),
         demand_mode: str = typer.Option("proxy_prev28",
             help="proxy_prev28 (default, no future info) | actual_month (fair replay) | master")):
    """Produce a fresh schedule for one month using the latest learn run."""
    from planner.plan.demand import DemandMode
    from planner.replay.harness import replay_month, _latest_learn_run
    from planner.runs.run_context import RunContext
    y, m = [int(x) for x in month.split("-")]
    rc = RunContext.new(tag=f"plan-{y}{m:02d}")
    out = rc.dir / f"month={y}-{m:02d}"
    out.mkdir(parents=True, exist_ok=True)
    result = replay_month(y, m, _latest_learn_run(), out,
                          demand_mode=DemandMode(demand_mode))
    log.info("cli.plan.done", result=result)


@app.command()
def optimize(
    run_id: str = typer.Option(None, help="run_id to optimize (defaults to latest plan run)"),
    budget: float = typer.Option(600.0, help="wall-clock seconds for SA loop"),
):
    """SA + Tabu + LNS over the latest plan schedule."""
    from planner.config import CONFIG
    from planner.optimize.driver import run as run_optimize
    if run_id is None:
        cands = sorted([p for p in CONFIG.paths.runs.iterdir()
                        if p.is_dir() and "plan-" in p.name])
        if not cands:
            log.error("cli.optimize.no_plan_run")
            raise typer.Exit(1)
        run_id = cands[-1].name
    out = run_optimize(CONFIG.paths.runs / run_id, budget_seconds=budget)
    log.info("cli.optimize.done", out=str(out))


@app.command()
def sim(
    run_id: str = typer.Option(None, help="run_id to simulate (defaults to latest plan run)"),
    n_reps: int = typer.Option(200, help="replications"),
):
    """SimPy stochastic evaluation of a planner-produced schedule."""
    import polars as pl
    from planner.config import CONFIG
    from planner.replay.harness import _latest_learn_run
    from planner.simulate.discrete_event import run_replications
    if run_id is None:
        cands = sorted([p for p in CONFIG.paths.runs.iterdir()
                        if p.is_dir() and "plan-" in p.name])
        if not cands:
            log.error("cli.sim.no_plan_run")
            raise typer.Exit(1)
        run_id = cands[-1].name
    month_dirs = sorted((CONFIG.paths.runs / run_id).glob("month=*"))
    if not month_dirs:
        log.error("cli.sim.no_month_dir")
        raise typer.Exit(1)
    md = month_dirs[-1]
    build_df = pl.read_parquet(md / "build_schedule.parquet")
    cure_p = md / "cure_schedule.parquet"
    cure_df = pl.read_parquet(cure_p) if cure_p.exists() else pl.DataFrame()
    out = run_replications(build_df, cure_df,
                           _latest_learn_run() / "learn",
                           md / "sim",
                           n_reps=n_reps)
    log.info("cli.sim.done", out=str(out))


@app.command()
def validate(
    run_id: str = typer.Option(None, help="run_id to validate (defaults to latest plan run)"),
    fuzz: bool = typer.Option(False, help="also run the fuzz suite"),
):
    """Independent hard-rule verifier + optional fuzz."""
    from planner.config import CONFIG
    from planner.validate.violations import verify_and_write
    from planner.validate.fuzz import run_all_fuzz
    if run_id is None:
        cands = sorted([p for p in CONFIG.paths.runs.iterdir()
                        if p.is_dir() and "plan-" in p.name])
        if not cands:
            log.error("cli.validate.no_plan_run")
            raise typer.Exit(1)
        run_id = cands[-1].name
    month_dirs = sorted((CONFIG.paths.runs / run_id).glob("month=*"))
    if not month_dirs:
        log.error("cli.validate.no_month_dir")
        raise typer.Exit(1)
    md = month_dirs[-1]
    out = verify_and_write(md)
    log.info("cli.validate.done", out=str(out))
    if fuzz:
        r = run_all_fuzz(CONFIG.paths.runs / run_id / "fuzz")
        log.info("cli.validate.fuzz", results=r)
        if not all(r.values()):
            raise typer.Exit(2)


if __name__ == "__main__":
    app()
