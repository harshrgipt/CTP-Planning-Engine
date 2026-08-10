"""Run ONE A/B arm end to end, fresh, substituting the PROTOTYPE L5.

    python scripts/_diag_run_arm.py <arm> <month> [KEY=VALUE ...]

Identical to scripts/run_arm.py except stage 1 is `planner.cmbc._diag_l5_level`
instead of `planner.cmbc.l5_cure_master`. Every arm is a fresh directory built
L5 -> L11 in order (DO-NOT 8 / 18): never seeded from another run, so the
scorecard beside it always describes the plan beside it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

STAGES = ["_diag_l5_level", "l6_build_gate", "l7_pull_release",
          "l8_prep_explosion", "l10_discretise", "l11_validate_plan"]


def main() -> int:
    name, month = sys.argv[1], sys.argv[2]
    env_over = dict(x.split("=", 1) for x in sys.argv[3:] if "=" in x)
    run = ROOT / "runs" / name
    if run.exists():
        shutil.rmtree(run)
    env = {**os.environ, **env_over, "PYTHONIOENCODING": "utf-8"}
    print(f"ARM {name}  month={month}  "
          + ("  ".join(f"{k}={v}" for k, v in env_over.items()) or "(defaults)"))
    for st in STAGES:
        flag = "--out" if st.endswith("l5_level") else "--run"
        cp = subprocess.run([PY, "-m", f"planner.cmbc.{st}", "--month", month,
                             flag, name], cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
        if cp.returncode != 0:
            sys.stderr.write(cp.stdout[-4000:] + "\n" + cp.stderr[-4000:] + "\n")
            raise SystemExit(f"!! arm {name}: {st} failed ({cp.returncode})")
        (run / f"log_{st}.txt").write_text(cp.stdout, encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    from planner.cmbc.l11_validate_plan import arm_is_stale
    why = arm_is_stale(run)
    if why:
        raise SystemExit(f"!! arm {name} STALE after a fresh run: {why}")
    print(f"  -> runs/{name}  FRESH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
