"""Run ONE month of the leak-free walk-forward and print its KPI row.

    PYTHONPATH=<root> python scripts/run_month.py 2026-01 runs/<accumulating-dir>

Learns from `date < month`, plans the month, then scores against actuals.
Reuses an existing `learn-asof<date>` KB if one is already on disk, so
re-running a month to test a fix does not re-pay the mining cost.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import date
from pathlib import Path

from planner.config import CONFIG
from planner.data.warehouse import set_cutoff
from planner.learn.rule_extract import run_learn
from planner.replay.full_kpi import compute_month
from planner.runs.logger import log

from walkforward import plan_month


def learn_for(cutoff: date, *, force: bool = False) -> Path:
    """Return the KB directory mined from `date < cutoff`, building it if needed."""
    want = f"learn-asof{cutoff.isoformat()}"
    if not force:
        hits = sorted(p for p in CONFIG.paths.runs.iterdir()
                      if p.is_dir() and want in p.name and (p / "rules.duckdb").exists())
        if hits:
            log.info("month.learn.reused", run=hits[-1].name)
            return hits[-1]
    t0 = time.time()
    kb = run_learn(cutoff=cutoff)
    log.info("month.learn.built", run=kb.parent.name, secs=round(time.time() - t0, 1))
    return kb.parent


def main() -> None:
    month = sys.argv[1]
    out_root = Path(sys.argv[2])
    force = "--force-learn" in sys.argv
    y, m = (int(x) for x in month.split("-"))
    cutoff = date(y, m, 1)

    out_root.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    learn_run = learn_for(cutoff, force=force)
    with (learn_run / "rules.json").open(encoding="utf-8") as fh:
        rules = json.load(fh)
    rule_mix: dict[str, int] = {}
    for r in rules:
        rule_mix[r.get("type", "?")] = rule_mix.get(r.get("type", "?"), 0) + 1

    # Several artefacts (demand_shortfall, starvation_unresolved) are only
    # written when non-empty, so a stale file from a previous run would be read
    # back as if it were this run's result. Start clean.
    month_dir = out_root / f"month={y}-{m:02d}"
    if month_dir.exists():
        shutil.rmtree(month_dir)
    plan_month(y, m, learn_run, month_dir, cutoff=cutoff)

    set_cutoff(None)
    row = compute_month(month_dir)
    row["_rules_total"] = len(rules)
    row["_rules_mix"] = rule_mix
    row["_learn_run"] = learn_run.name
    row["_elapsed_s"] = round(time.time() - t0, 1)

    (month_dir / "kpi_row.json").write_text(json.dumps(row, indent=2, default=str),
                                           encoding="utf-8")
    print("MONTH_RESULT " + json.dumps(row, default=str))


if __name__ == "__main__":
    main()
