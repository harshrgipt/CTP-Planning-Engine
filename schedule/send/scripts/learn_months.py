"""Walk the history month by month, run all 16 miners, write LEARNING.md.

    python scripts/learn_months.py

Starts at December and adds one month at a time, so snapshot k has seen exactly
the first k months and nothing after. That is the point: a KB that has seen the
month it is planning is not a KB, it is an answer key.

LEARNING.md records, per snapshot, what each miner found AND what changed since
the previous snapshot -- because the useful question is not "what is the value"
but "has it settled". A quantity still moving after 6 months must be derived per
run; one that settled after 2 can be trusted.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.learn.miners import MINERS, build_pairs
from planner.runs.logger import log


def _months() -> list[date]:
    rows = duck().execute(
        "SELECT DISTINCT date_trunc('month', event_ts)::DATE FROM v_build "
        "WHERE stage = 2 ORDER BY 1").fetchall()
    return [r[0] for r in rows if r[0]]


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def main(out: Path) -> int:
    set_cutoff(None)          # miners filter by `asof` themselves
    build_pairs()
    months = _months()
    log.info("learn.months", n=len(months), first=str(months[0]), last=str(months[-1]))

    lines: list[str] = []
    lines.append("# LEARNING LOG — what the plant taught us, month by month\n")
    lines.append("Cumulative walk-forward. Snapshot *k* has seen the first *k* "
                 "months and **nothing after**. Sixteen miners: the nine "
                 "specified, plus seven added because this engine has already "
                 "been burned by not having them.\n")
    lines.append("| # | Miner | Why it exists |")
    lines.append("|---|---|---|")
    why = {
        "10": "the plant holds `I = λ·W`, W≈9h. Without a setpoint WIP climbed 4× and no KPI saw it",
        "11": "72h shelf life is the binding HARD rule; 6.9% of one plan was scrap, unreported",
        "12": "`build/cure − 1` is LOSS, not drift. Target 1.000 and you under-deliver 0.5–2.0%",
        "13": "`M_g` caps `n_g`, so the rectangle model rests on it. We only had a lower bound",
        "14": "40–47% of pairs are NEW monthly. Gating on history starves the plan",
        "15": "Jan has a near-shutdown day (3,068 vs 12,666); 24×7 cannot represent it",
        "16": "99.89% — belongs in the candidate SET as a hard prefilter, not a score term",
    }
    for name, _fn in MINERS:
        num = name.split(".")[0]
        lines.append(f"| {num} | {name.split('. ',1)[1].replace('  [ADDED]','')} "
                     f"| {why.get(num, 'specified')} |")
    lines.append("")

    prev: dict[str, list[str]] = {}
    all_facts: dict[str, dict] = {}
    for i, mo in enumerate(months, start=1):
        asof = _next_month(mo)
        tag = f"{mo.year}-{mo.month:02d}"
        lines.append(f"\n---\n\n## Snapshot {i} — through {tag} "
                     f"(as-of {asof}, {i} month{'s' if i > 1 else ''} seen)\n")
        snap: dict = {}
        for name, fn in MINERS:
            try:
                facts, ins = fn(asof)
            except Exception as e:  # noqa: BLE001
                log.warning("learn.miner_failed", miner=name, month=tag, err=str(e))
                continue
            if not ins:
                continue
            snap[name] = facts
            lines.append(f"**{name}**\n")
            for s in ins:
                lines.append(f"- {s}")
            old = prev.get(name)
            if old is not None:
                new = [s for s in ins if s not in old and not s.startswith("=>")]
                if new:
                    lines.append(f"  - *changed since last snapshot:* {len(new)} "
                                 f"measurement(s) moved")
            lines.append("")
            prev[name] = ins
        all_facts[tag] = snap
        print(f"  snapshot {i}: through {tag} -- {len(snap)} miners reported")

    lines.append("\n---\n\n## What is still MISSING and cannot be mined\n")
    lines.append("| Gap | Consequence today |")
    lines.append("|---|---|")
    for g, c in [
        ("Press platen master (rim range per press)",
         "eligibility is history-derived; press matrix lists 114 PCR presses vs ~87 real"),
        ("Machine certification list",
         "median GT shows only 2 eligible machines — the engine must override it to plan at all"),
        ("True mould count `M_g`",
         "we infer a LOWER bound; `M_g` caps `n_g`, so the rectangle model rests on it"),
        ("Plant calendar / planned downtime",
         "24×7 assumed; real low-production days are invisible"),
        ("Customer demand file",
         "ours is derived from the same month's output, so planning that month is in-sample"),
        ("Bladder / PM / breakdown log",
         "no downtime model, so robustness cannot be tested"),
    ]:
        lines.append(f"| {g} | {c} |")

    out.write_text("\n".join(lines), encoding="utf-8")
    (out.parent / "learning_facts.json").write_text(
        json.dumps(all_facts, indent=2, default=str), encoding="utf-8")
    print(f"\nWROTE {out}  ({out.stat().st_size/1024:.0f} KB)")
    log.info("learn.done", snapshots=len(months))
    return 0


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG.paths.root / "LEARNING.md"
    sys.exit(main(p))
