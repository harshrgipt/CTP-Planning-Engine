"""Drive the load-allocation A/B: rebuild the partition in a named mode, then
run a FRESH arm against it.  Read-only w.r.t. the shipped layers.

    python scripts/_diag_alloc_sweep.py <month> <arm> <part-mode> [KEY=VAL ...]

`part-mode` is off | free | merge | bal | tyres and is passed to
`scripts/_diag_build_partition.py`; `off` uses the SHIPPED builder so the
baseline is the shipped object, not a diag reproduction of it.

The partition file is a single shared input, so an arm that does not rebuild it
inherits whatever the previous arm left there -- which is exactly the DO-NOT #8
class one layer further out.  Every arm here rebuilds.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def build_partition(month: str, mode: str) -> None:
    script = ("scripts/build_gt_machine_partition.py" if mode == "off"
              else "scripts/_diag_build_partition.py")
    env = {**os.environ, "PYTHONPATH": ".", "PYTHONIOENCODING": "utf-8",
           "PLANNER_PART_BALANCE": mode}
    cp = subprocess.run([PY, script, month], cwd=ROOT, env=env,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout[-3000:] + cp.stderr[-3000:])
        raise SystemExit(f"!! partition build failed (mode={mode})")
    for ln in cp.stdout.splitlines():
        if "machine load" in ln or "UNSEATED" in ln or "MORE THAN ONE" in ln:
            print("   " + ln.strip())


def main() -> int:
    month, arm, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    over = [a for a in sys.argv[4:] if "=" in a]
    print(f"ARM {arm}  month={month}  partition={mode}  {' '.join(over) or '(defaults)'}")
    build_partition(month, mode)
    cp = subprocess.run(
        [PY, "scripts/run_arm.py", arm, "--month", month, *over],
        cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(cp.stdout[-1500:])
    if cp.returncode != 0:
        sys.stderr.write(cp.stderr[-3000:])
        return cp.returncode
    (ROOT / "runs" / arm / "ARM_PROVENANCE.txt").write_text(
        f"month={month}\npartition_mode={mode}\nenv={' '.join(over)}\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
