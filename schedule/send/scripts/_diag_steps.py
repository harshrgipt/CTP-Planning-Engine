"""Fulfilment-per-plant summary across cumulative arms. Read-only.

    python scripts/_diag_steps.py 2026-07 base_jul s1_jul s2_jul ...

Uses arm_report.measure(), so every number is recomputed from
build_schedule.parquet / cure_campaigns_reconciled.parquet and any arm whose
scorecard does not describe the plan beside it is refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from arm_report import measure  # noqa: E402

KEYS = [("ful", "{:.2f}"), ("fed", "{:,.0f}"), ("starved", "{:,.0f}"),
        ("l5_unplaced", "{:,.0f}"), ("subfloor", "{:.1f}"),
        ("lot_p50", "{:,.0f}"), ("setups", "{:,.0f}"),
        ("co_per_md", "{:.2f}"), ("setup_h", "{:,.0f}"),
        ("same_pct", "{:.1f}"), ("inv_mean", "{:,.0f}"),
        ("inv_daymax", "{:,.0f}"), ("inv_lastday", "{:,.0f}"),
        ("r5_max", "{:.1f}"), ("idle_h", "{:,.0f}")]


def main() -> int:
    month = sys.argv[1]
    arms = sys.argv[2:]
    res = [measure(ROOT / "runs" / a, month) for a in arms]
    w = max(12, max(len(a) for a in arms) + 2)
    for p in ("PCR", "TBR"):
        print(f"-- {p}  {month} " + "-" * 40)
        print(f"{'metric':<14}" + "".join(f"{r['run']:>{w}}" for r in res))
        for k, f in KEYS:
            print(f"{k:<14}" + "".join(
                f"{f.format(r[p][k]):>{w}}" if k in r[p] else f"{'-':>{w}}"
                for r in res))
    print(f"{'L11 PASS':<14}" + "".join(
        f"{str(r['l11_pass']) + '/' + str(r['l11_n']):>{w}}" for r in res))
    base = res[0]
    for r in res[1:]:
        fl = [(k, base["l11"].get(k, ("-", "-"))[1], v[1], v[0])
              for k, v in r["l11"].items()
              if base["l11"].get(k, ("-", "-"))[1] != v[1]]
        print(f"\n  {r['run']} vs {base['run']}: {len(fl)} flip(s)")
        for k, o, n, act in fl:
            print(f"    {o}->{n:<5} {k}  (now {act})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
