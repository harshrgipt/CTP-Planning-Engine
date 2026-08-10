"""THE PLANNING ENGINE CLI. Demand + inventory in, executable month out.

    python scripts/plan.py --demand masters/demand/demand_2026-07.csv \
                           --opening masters/opening_gt/opening_gt_2026-07.parquet \
                           --month 2026-07 \
                           --out output/engine/2026-07

Deterministic: the same demand + inventory + masters produce a byte-identical
plan. Re-running is the regression test.

Phases
    P0 contract      validate, normalise, hash
    P1 masters       derive rate / eligibility / scrap from measured data
    P2 gate          C1-C4; stops if no scheduler can help
    P3 campaigns     press plan (strip packing at fixed area)
    P4 controller    base-stock build targets
    P5 building      machine + sequence
    P6 grid          31 x 3 x 480 shift execution
    P7 converge      fixed point on realised cure
    P8 verify        independent, includes shelf life + inventory trend
    P9 emit          parquet + CTP workbooks + KPI ladder
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from datetime import date
from pathlib import Path

from planner.config import CONFIG
from planner.data.warehouse import set_cutoff
from planner.engine.contract import load_request
from planner.engine.pipeline import run_engine
from planner.engine.verify import kpis, verify
from planner.learn.rule_extract import run_learn
from planner.runs.logger import log


def _kb_for(cutoff: date) -> Path:
    want = f"learn-asof{cutoff.isoformat()}"
    hits = sorted(p for p in CONFIG.paths.runs.iterdir()
                  if p.is_dir() and want in p.name and (p / "rules.duckdb").exists())
    if hits:
        log.info("engine.kb.reused", run=hits[-1].name)
        return hits[-1]
    log.info("engine.kb.mining", cutoff=str(cutoff))
    return run_learn(cutoff=cutoff).parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demand", required=True)
    ap.add_argument("--opening", default=None)
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out", default=None)
    ap.add_argument("--kb", default=None, help="learn run dir; mined if absent")
    ap.add_argument("--no-export", action="store_true")
    a = ap.parse_args()

    y, m = (int(x) for x in a.month.split("-"))
    start, end = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    out = Path(a.out) if a.out else CONFIG.paths.root / "output" / "engine" / a.month
    t0 = time.time()

    # The KB may only see history BEFORE the horizon, or the plan is not a plan.
    set_cutoff(start)
    kb = Path(a.kb) if a.kb else _kb_for(start)

    req = load_request(Path(a.demand),
                       Path(a.opening) if a.opening else None, start, end)
    if req.rejected:
        print(f"REJECTED {len(req.rejected)} rows -- see input_validation.json")
    out.mkdir(parents=True, exist_ok=True)
    (out / "input_validation.json").write_text(
        json.dumps({"rejected": req.rejected, "quarantined": req.quarantined,
                    "notes": req.notes}, indent=2, default=str))

    report = run_engine(req, kb, out)
    if not report["feasibility"]["go"]:
        print("INFEASIBLE -- see feasibility in run_report.json")
        for p, v in report["feasibility"]["plants"].items():
            if v.get("C1_infeasible_gts"):
                print(f"  {p}: {len(v['C1_infeasible_gts'])} GTs cannot fit at any "
                      f"aspect ratio (mould count / eligibility problem)")
        return 2

    set_cutoff(None)
    verify(out)
    row = kpis(out, req)
    row["run_id"] = report.get("run_id")
    row["passes"] = len(report.get("passes", []))
    row["elapsed_s"] = round(time.time() - t0, 1)
    (out / "kpi.json").write_text(json.dumps(row, indent=2, default=str))

    if not a.no_export:
        try:
            from scripts.export_ctp import export
            export(a.month, out, out)
        except Exception as e:  # noqa: BLE001
            log.warning("engine.export_failed", err=str(e))

    print("ENGINE_RESULT " + json.dumps(row, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
