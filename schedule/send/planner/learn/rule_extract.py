"""Orchestrator for Phase 1 → Phase 2. Runs all miners and persists rules."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from planner.config import CONFIG
from planner.kb.promoter import promote
from planner.kb.rule_store import export_json, open_store, save_rules
from planner.kb.rule_types import Rule
from planner.learn.aging import compute_aging
from planner.learn.balance_signal import compute_balance_signal
from planner.learn.calendar_infer import infer_calendar
from planner.learn.descriptive import compute_baselines
from planner.learn.machine_pref import build_mpm
from planner.learn.sequence_mining import mine_sequences
from planner.learn.sister_sku import cluster_sisters
from planner.learn.test_freq import compute_test_frequency
from planner.learn.timing import compute_timing
from planner.learn.violation_scan import scan_violations
from planner.runs.logger import log
from planner.runs.run_context import RunContext


def run_learn(cutoff: date | None = None) -> Path:
    """Mine rules from the warehouse.

    `cutoff` makes the run strictly out-of-sample for anything at or after that
    date: it is applied at the view layer, so every miner below sees only
    `date < cutoff`. Leave it None to learn from all available history.
    """
    from planner.data.warehouse import set_cutoff
    set_cutoff(cutoff)

    tag = "learn" if cutoff is None else f"learn-asof{cutoff.isoformat()}"
    rc = RunContext.new(tag=tag)
    learn_dir = rc.dir / "learn"
    learn_dir.mkdir(parents=True, exist_ok=True)

    log.info("learn.start", run_id=rc.run_id, cutoff=str(cutoff))

    compute_baselines(learn_dir / "baselines")
    scan_violations(learn_dir / "violations")

    all_rules: list[Rule] = []
    _, r = build_mpm(learn_dir / "mpm")
    all_rules += r
    _, r = compute_timing(learn_dir / "timing")
    all_rules += r
    _, r = mine_sequences(learn_dir / "sequences")
    all_rules += r
    _, r = cluster_sisters(learn_dir / "sister")
    all_rules += r
    _, r = compute_aging(learn_dir / "aging")
    all_rules += r

    all_rules += compute_balance_signal()
    all_rules += compute_test_frequency()
    infer_calendar(learn_dir / "calendar")

    all_rules = promote(all_rules)
    kb_path = rc.dir / "rules.duckdb"
    con = open_store(kb_path)
    n = save_rules(con, all_rules)
    export_json(con, rc.dir / "rules.json")

    # Summary
    from collections import Counter
    types = Counter(r.type.value for r in all_rules)
    log.info("learn.done", run_id=rc.run_id, rules=n, types=dict(types), path=str(kb_path))
    return kb_path
