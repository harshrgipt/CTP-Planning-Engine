"""Run ONE A/B arm end to end, fresh, with the environment it is meant to test.

    python scripts/run_arm.py <arm-name> [--month 2026-07] [KEY=VALUE ...]

    python scripts/run_arm.py base       --month 2026-07
    python scripts/run_arm.py nolock     PLANNER_HARD_LOCK=0
    python scripts/run_arm.py t24        PLANNER_LOT_INTERVAL_H=24

WHY THIS SCRIPT EXISTS -- it is the fix for a measurement defect, not a convenience.

Until now there was no driver. Arms were produced by hand as
`cp -r runs/<prev> runs/<new>` (the seeding step in PARTITION_AND_CHANGEOVER §7,
which exists to inherit L1-L6 artefacts) followed by re-running L7 only. That
copies the PREVIOUS arm's `l11_invariants.parquet` into the new directory, where
it is indistinguishable from a real result.

Measured 2026-08-08: **15 run directories carried another arm's scorecard.**
Eight shared `runs/v31`'s (`hl_00 hl_01 hl_10 hl_11 rr_0_0 rr_4800_1400 perplant`)
and seven shared `runs/v29`'s (`sm_2.0_3.0 sm_3.5_6.0 sm_3.5_8.0 sm_4.5_8.0
fin_0.94 fin_0.96`). Reading them, every HARD_LOCK arm scored an identical
93.6 % / 96.1 % same-size / 22-of-34 PASS -- while `build_starved.parquet` in the
same directories showed PCR starvation moving 13,743 -> 5,336. A flag worth
8,085 tyres read as free.

(The fulfilment and starvation columns quoted in the ledger were computed from
the arm-specific artefacts and are sound. Every INVARIANT read from those
directories -- same-size, weighted CO, n_g, GT inventory, R5, sub-floor, PASS
count -- belongs to a different arm.)

THE RULE THIS ENFORCES
  An arm is a FRESH directory built by running L5 -> L11 in order. L1-L4.5
  artefacts are month-level and live in `warehouse/derived/`, shared by every
  arm, so nothing needs copying and `cp -r` is never required.

  DO-NOT #8 already said "never A/B against an older run directory" because
  `RunContext` hashes config but not `PLANNER_*` env flags. This is the same
  class one layer down: the flag DID reach L7; the scoring layer never saw it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# L5 -> L11. L9 is skipped: it OVERWRITES cure_campaigns.parquet, which is L5's
# own output, so running it makes the arm no longer describe the plan L5 built.
#
# L6 and L8 were dropped 2026-08-11 and moved to planner/cmbc/_retired/. L7 never
# read l6_infeasible/l6_build_load (it runs its own B16 gate) and nothing at all
# read prep_requirement. Verified on 2026-08: removing both leaves every plan
# artefact bit-identical -- build_schedule, cure_campaigns, gt_events,
# cure_campaigns_reconciled, build_starved, build_by_shift, cure_by_shift,
# mould_changes and l11_invariants all compare equal, and the BTP pack matches
# the shipped one on all 34 sheets.
STAGES = ["l5_cure_master", "l7_pull_release", "l10_discretise",
          "l11_validate_plan"]


def run_arm(name: str, month: str, env_over: dict[str, str],
            *, quiet: bool = True) -> Path:
    run = ROOT / "runs" / name
    if run.exists():
        shutil.rmtree(run)               # FRESH. Never inherit a previous arm.
    env = {**os.environ, **env_over, "PYTHONIOENCODING": "utf-8"}
    for st in STAGES:
        # L5 names its destination `--out`; every later layer calls it `--run`.
        flag = "--out" if st == "l5_cure_master" else "--run"
        cp = subprocess.run(
            [PY, "-m", f"planner.cmbc.{st}", "--month", month, flag, name],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if cp.returncode != 0:
            sys.stderr.write(cp.stdout[-4000:] + "\n" + cp.stderr[-4000:] + "\n")
            raise SystemExit(f"!! arm {name}: {st} failed ({cp.returncode})")
        # Keep every stage's stdout beside the arm. The partition staleness
        # guard, the rim-spill report and the carry-in line are all printed, not
        # written to parquet -- without this there is no evidence the arm used
        # the month's own partition rather than falling back.
        (run / f"log_{st}.txt").write_text(cp.stdout, encoding="utf-8")
        if not quiet:
            print(cp.stdout)
    return run


def main() -> int:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    name = args[0]
    month = "2026-07"
    env_over: dict[str, str] = {}
    i = 1
    while i < len(args):
        if args[i] == "--month":
            month = args[i + 1]
            i += 2
        elif "=" in args[i]:
            k, v = args[i].split("=", 1)
            env_over[k] = v
            i += 1
        else:
            i += 1
    over = "  ".join(f"{k}={v}" for k, v in env_over.items()) or "(defaults)"
    print(f"ARM {name}  month={month}  {over}")
    run = run_arm(name, month, env_over, quiet=True)

    # Prove it, rather than assume it.
    sys.path.insert(0, str(ROOT))
    from planner.cmbc.l11_validate_plan import arm_is_stale
    why = arm_is_stale(run)
    if why:
        raise SystemExit(f"!! arm {name} is STALE after a fresh run: {why}")
    print(f"  -> runs/{name}  FRESH (scorecard provenance verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
