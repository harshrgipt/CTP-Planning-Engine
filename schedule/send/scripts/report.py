"""Build the final KPI report: out-of-sample panel, and the optimism gap.

Usage: python scripts/report.py <oos_run_dir> [insample_run_dir]

Emits report.json + report.md into the OOS run dir. The gap between the two
panels is the overfitting measurement: in-sample is the leaky protocol (KB
mined from all 8 months, then asked to "predict" months it already saw),
out-of-sample re-learns from scratch before each month.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from planner.data.warehouse import set_cutoff
from planner.replay.full_kpi import compute_run

# (key, label, direction) -- direction 1 = higher is better, -1 = lower is better,
# 0 = neutral/context only.
PANEL = [
    ("demand_qty",                  "Demand qty",                    0),
    ("demand_fulfillment_pct",      "Demand fulfillment %",          1),
    ("sync_pct",                    "Build-cure sync %",             1),
    ("makespan_hours",              "Makespan (h)",                 -1),
    ("machine_util_pct",            "Machine util %",                1),
    ("press_util_pct",              "Press util %",                  1),
    ("on_time_pct",                 "On-time %",                     1),
    ("building_changeovers",        "Building changeovers",         -1),
    ("curing_changeovers",          "Curing changeovers",           -1),
    ("gt_aging_p95_hours",          "GT aging p95 (h)",             -1),
    ("avg_wip",                     "Avg WIP",                      -1),
    ("machine_idle_hours",          "Machine idle (h)",             -1),
    ("press_idle_hours",            "Press idle (h)",               -1),
    ("daily_production_cv",         "Daily production CV",          -1),
    ("hard_violations",             "Hard violations",              -1),
    ("machine_sku_stickiness_pct",  "Machine-SKU stickiness %",      1),
    ("size_lock_pct",               "Size lock %",                   1),
    ("avg_skus_per_machine_day",    "Avg SKUs / machine-day",       -1),
    ("starvation_events",           "Starvation events",            -1),
    ("demand_shortfall",            "Demand shortfall",             -1),
    ("actual_changeovers",          "ACTUAL changeovers (history)",  0),
    ("actual_util_pct",             "ACTUAL machine util %",         0),
    ("actual_gt_aging_p95_hours",   "ACTUAL GT aging p95 (h)",       0),
]

# Metrics where the planner is scored head-to-head against real plant history.
# (planner_key, actual_key, direction, label)
HEAD_TO_HEAD = [
    ("building_changeovers", "actual_changeovers",       -1, "Changeovers"),
    ("machine_util_pct",     "actual_util_pct",           1, "Machine util %"),
    ("gt_aging_p95_hours",   "actual_gt_aging_p95_hours", -1, "GT aging p95 (h)"),
]


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e4 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _table(rows: list[dict], title: str) -> str:
    if not rows:
        return f"### {title}\n\n_no months produced_\n"
    months = [r["month"] for r in rows]
    out = [f"### {title}", "", "| KPI | " + " | ".join(months) + " |",
           "|---|" + "---:|" * len(months)]
    for key, label, _d in PANEL:
        cells = [_fmt(r.get(key)) for r in rows]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _head_to_head(rows: list[dict]) -> tuple[str, dict]:
    """Score planner vs actual plant history, per month, on real metrics only."""
    lines = ["### Planner vs actual plant history "
             "(planner value vs what the plant actually did)", "",
             "| Month | " + " | ".join(lab for _pk, _ak, _d, lab in HEAD_TO_HEAD) + " | Wins |",
             "|---|" + "---:|" * (len(HEAD_TO_HEAD) + 1)]
    tally = {pk: 0 for pk, _ak, _d, _lab in HEAD_TO_HEAD}
    counted = 0
    for r in rows:
        cells, wins = [], 0
        for pk, ak, direction, _lab in HEAD_TO_HEAD:
            pv, av = r.get(pk), r.get(ak)
            if pv is None or av is None or av == 0:
                cells.append("-")
                continue
            better = (pv > av) if direction == 1 else (pv < av)
            wins += int(better)
            tally[pk] += int(better)
            cells.append(f"{_fmt(pv)} vs {_fmt(av)} {'W' if better else 'L'}")
        counted += 1
        lines.append(f"| {r['month']} | " + " | ".join(cells) + f" | {wins}/{len(HEAD_TO_HEAD)} |")
    return "\n".join(lines) + "\n", {"months": counted, "wins_by_metric": tally}


def _gap(oos: list[dict], ins: list[dict]) -> tuple[str, dict]:
    """Optimism gap: in-sample minus out-of-sample, on months both produced."""
    by_month_oos = {r["month"]: r for r in oos}
    by_month_ins = {r["month"]: r for r in ins}
    shared = sorted(set(by_month_oos) & set(by_month_ins))
    if not shared:
        return "_no overlapping months to compare_\n", {}

    lines = ["### Overfitting check: in-sample vs out-of-sample", "",
             f"Months compared: {', '.join(shared)}", "",
             "| KPI | In-sample (leaky) | Out-of-sample | Gap | Verdict |",
             "|---|---:|---:|---:|---|"]
    detail = {}
    for key, label, direction in PANEL:
        if direction == 0:
            continue
        iv = [by_month_ins[m].get(key) for m in shared]
        ov = [by_month_oos[m].get(key) for m in shared]
        if any(v is None for v in iv + ov):
            continue
        mi, mo = statistics.mean(iv), statistics.mean(ov)
        gap = mo - mi
        # Degradation = out-of-sample worse than in-sample.
        worse = (gap < 0) if direction == 1 else (gap > 0)
        rel = abs(gap) / abs(mi) * 100 if mi else 0.0
        if abs(gap) < 1e-9:
            verdict = "identical"
        elif worse:
            verdict = f"degrades {rel:,.1f}%"
        else:
            verdict = f"improves {rel:,.1f}%"
        detail[key] = {"in_sample_mean": mi, "out_of_sample_mean": mo,
                       "gap": gap, "rel_pct": rel, "degrades": bool(worse)}
        lines.append(f"| {label} | {_fmt(mi)} | {_fmt(mo)} | {_fmt(gap)} | {verdict} |")
    return "\n".join(lines) + "\n", detail


def main() -> None:
    set_cutoff(None)
    oos_dir = Path(sys.argv[1])
    ins_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    oos = compute_run(oos_dir)
    ins = compute_run(ins_dir) if ins_dir and ins_dir.exists() else []

    parts = ["# Full-history evaluation report", ""]
    parts.append(_table(oos, "Out-of-sample walk-forward (leak-free)"))
    h2h, h2h_summary = _head_to_head(oos)
    parts.append(h2h)
    gap_md, gap_detail = ("", {})
    if ins:
        parts.append(_table(ins, "In-sample (leaky baseline -- diagnostic only)"))
        gap_md, gap_detail = _gap(oos, ins)
        parts.append(gap_md)

    md = "\n".join(parts)
    (oos_dir / "report.md").write_text(md, encoding="utf-8")
    (oos_dir / "report.json").write_text(json.dumps({
        "out_of_sample": oos,
        "in_sample": ins,
        "head_to_head": h2h_summary,
        "overfitting_gap": gap_detail,
    }, indent=2, default=str), encoding="utf-8")
    print(md)
    print(f"\nWROTE {oos_dir / 'report.md'}")


if __name__ == "__main__":
    main()
