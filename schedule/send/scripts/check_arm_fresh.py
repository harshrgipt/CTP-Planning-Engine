"""Prove an A/B arm's scorecard describes the plan sitting beside it.

    python scripts/check_arm_fresh.py                 # every runs/* dir
    python scripts/check_arm_fresh.py v32 hl_01 ...   # named dirs
    python scripts/check_arm_fresh.py --quiet         # only the bad ones

Exit code 1 if any directory is stale, so it can gate a sweep.

WHY THIS EXISTS. On 2026-08-08 five directories (`runs/hl_00 hl_01 hl_10 hl_11`,
`runs/rr_*`) each carried an `l11_invariants.parquet` that was byte-identical to
`runs/v31`'s and written 1-2 SECONDS BEFORE their own `build_schedule.parquet`.
Every arm therefore read as 93.6 % fulfilment / 96.1 % same-size / 22-of-34 PASS,
while `build_starved.parquet` in the same directories showed PCR starvation
moving 13,743 -> 5,336. A change worth 8,085 tyres read as free.

The cause is the documented seeding step `cp -r runs/<prev> runs/<new>`
(PARTITION_AND_CHANGEOVER.md §7): it inherits L1-L6 artefacts, and also the
previous arm's L11 result. L7 is re-run, L11 is not, and the stale scorecard is
indistinguishable from a real one.

DO-NOT #8 already said "never A/B against an older run directory" because
`RunContext` hashes config but not `PLANNER_*` env flags. This is the same class
one layer down: the flag DID take effect in L7, and the scoring layer never saw
it. Run this before reading any number out of a sweep.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner.cmbc.l11_validate_plan import arm_is_stale  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    quiet = "--quiet" in sys.argv
    if args:
        dirs = [RUNS / a for a in args]
    else:
        dirs = sorted(d for d in RUNS.iterdir()
                      if d.is_dir() and (d / "l11_invariants.parquet").exists())

    bad, unproven, ok = [], [], []
    for d in dirs:
        why = arm_is_stale(d)
        if why is None:
            ok.append(d.name)
        elif why.startswith("no l11_provenance.json"):
            unproven.append(d.name)
        else:
            bad.append((d.name, why))

    print("=" * 88)
    print(f"ARM FRESHNESS  --  {len(dirs)} directories with a scorecard")
    print("=" * 88)
    for name, why in bad:
        print(f"  STALE     {name:<22} {why}")
    if not quiet:
        for name in unproven:
            print(f"  UNPROVEN  {name:<22} pre-guard run; re-run L11 to stamp it")
        for name in ok:
            print(f"  FRESH     {name}")
    print(f"\n  {len(ok)} fresh · {len(unproven)} unproven · {len(bad)} STALE")
    if bad:
        print("\n  Do not read numbers out of a STALE directory. Re-run:")
        print("      python -m planner.cmbc.l11_validate_plan --month <M> --run <arm>")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
