"""Entry point wiring: load a run's schedule + rules, optimize, persist."""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from planner.optimize.sa import optimize
from planner.optimize.state import Schedule
from planner.plan.rulekb import load_rules
from planner.runs.logger import log


def _allowed_machines(rules_path: Path) -> dict[str, list[str]]:
    kb = load_rules(rules_path)
    out: dict[str, list[str]] = {}
    for (_plant, _stage, sku), entries in kb.mpm.items():
        out.setdefault(sku, []).extend(sorted({e.machine for e in entries}))
    return out


def run(run_dir: Path, budget_seconds: float = 600.0) -> Path:
    month_dirs = sorted(run_dir.glob("month=*"))
    if not month_dirs:
        raise FileNotFoundError(f"no month=*/ dir under {run_dir}")
    month_dir = month_dirs[-1]

    build_df = pl.read_parquet(month_dir / "build_schedule.parquet")
    sched = Schedule.from_build_df(build_df)

    # Find the learn run for KB lookup.
    from planner.config import CONFIG
    learn_runs = sorted([p for p in CONFIG.paths.runs.iterdir()
                        if p.is_dir() and "learn" in p.name])
    if not learn_runs:
        raise FileNotFoundError("no learn run")
    rules_path = learn_runs[-1] / "rules.duckdb"
    allowed = _allowed_machines(rules_path)

    demand_qty = float(build_df["qty"].sum())
    scheduled_qty = demand_qty  # placeholder — planner already scheduled all lots
    result = optimize(sched,
                      allowed_machines=allowed,
                      demand_qty=demand_qty,
                      scheduled_qty=scheduled_qty,
                      budget_seconds=budget_seconds)

    out_dir = month_dir / "optimize"
    out_dir.mkdir(parents=True, exist_ok=True)
    sched.to_polars().write_parquet(out_dir / "build_schedule.parquet", compression="zstd")
    (out_dir / "sa_result.json").write_text(json.dumps({
        "best_score": result.best_score,
        "best_terms": result.best_terms,
        "seed_score": result.seed_score,
        "iters": result.iters,
        "accepted": result.accepted,
        "improved": result.improved,
        "delta_pct": (result.seed_score - result.best_score) / max(result.seed_score, 1e-6) * 100,
    }, indent=2))

    # Persist history (small).
    if result.history:
        hist_df = pl.DataFrame({
            "iter": [h[0] for h in result.history],
            "T": [h[1] for h in result.history],
            "score": [h[2] for h in result.history],
            "accepted": [h[3] for h in result.history],
        })
        hist_df.write_parquet(out_dir / "history.parquet", compression="zstd")

    log.info("optimize.done", dir=str(out_dir),
             seed_score=result.seed_score, best_score=result.best_score,
             delta_pct=(result.seed_score - result.best_score) / max(result.seed_score, 1e-6) * 100)
    return out_dir
