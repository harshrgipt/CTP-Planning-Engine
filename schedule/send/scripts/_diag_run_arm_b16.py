"""Diagnostic arm driver: same STAGES as run_arm.py but L7 swapped for the
B16 hard-lock diagnostic copy (planner/cmbc/_diag_l7_b16.py).

    python scripts/_diag_run_arm_b16.py <arm> --month 2026-07 PLANNER_B16_HARD=1

Shipped layers are untouched; this only substitutes the module name.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib.util as _iu  # noqa: E402
_sp = _iu.spec_from_file_location('_ra', Path(__file__).resolve().parent / 'run_arm.py')
ra = _iu.module_from_spec(_sp); _sp.loader.exec_module(ra)

ra.STAGES = ["l5_cure_master", "l6_build_gate", "_diag_l7_b16",
             "l8_prep_explosion", "l10_discretise", "l11_validate_plan"]

if __name__ == "__main__":
    raise SystemExit(ra.main())
