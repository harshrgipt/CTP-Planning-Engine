"""SINGLE RESOLVER for every input file the engine reads.

    from planner import paths
    paths.raw("wcmaster 1.xlsx")            -> INPUT/raw/wcmaster 1.xlsx
    paths.input_derived("gt_size.parquet")  -> INPUT/derived/gt_size.parquet
    paths.wh_derived("cap_machine_2026-08.parquet")

WHY THIS FILE EXISTS
  Until now every module spelled its own `ROOT.parent.parent / "INPUT" / ...`,
  and the plant workbooks sat loose at the wrapper root next to README.md. That
  is ~30 hand-written path expressions with no single place to change when the
  data moves, and it is why `ROOT.parent.parent` is load-bearing in a way no
  new reader expects. All input lookups now go through here.

TWO `derived` NAMESPACES -- THEY ARE NOT INTERCHANGEABLE
  `INPUT/derived/`     the frozen, shipped master set (mined once, committed)
  `warehouse/derived/` artefacts the engine itself writes per month

  21 filenames exist in BOTH. Twenty are byte-identical, and
  **`press_mould_change.parquet` IS NOT** -- L5 and L10 read the warehouse copy
  and would silently change behaviour if a resolver fell back to the INPUT one.
  So `input_derived()` and `wh_derived()` are separate functions with NO
  cross-fallback. Do not "simplify" them into one.

LEGACY FALLBACK
  `raw()`, `cycletime()`, `opening_gt_manual()` and `btpformat()` fall back to
  the pre-consolidation locations at the wrapper root, so an older checkout
  still runs. The ten files that moved were verified byte-identical to the
  copies already in `INPUT/raw/` before the originals were removed.

OVERRIDE
  `PLANNER_DATA_ROOT` relocates the whole INPUT tree (frontend deployments that
  mount data elsewhere). Unset, it is the wrapper root two levels above
  `planner/`, exactly as before.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# roots
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent            # schedule/send
DATA_ROOT = Path(os.environ.get("PLANNER_DATA_ROOT", str(ROOT.parent.parent)))

INPUT = DATA_ROOT / "INPUT"

RAW = INPUT / "raw"                       # plant workbooks, verbatim
INPUT_DERIVED = INPUT / "derived"         # frozen mined masters
DEMAND = INPUT / "demand"                 # archived demand by month
OPENING_GT = INPUT / "opening_gt"         # archived opening stock by month
CYCLETIME = INPUT / "cycletime"           # plant cycle-time workbooks
OPENING_GT_MANUAL = INPUT / "opening_gt_manual"   # manual floor counts
BTPFORMAT = INPUT / "btpformat"           # target workbook shape, reference only

# engine-internal trees (inside schedule/send) -- never relocated by DATA_ROOT
WAREHOUSE = ROOT / "warehouse"
WH_DERIVED = WAREHOUSE / "derived"
WH_PARAMS = WAREHOUSE / "params"
WH_MASTERS = WAREHOUSE / "masters"
MASTERS = ROOT / "masters"
RUNS = ROOT / "runs"
OUTPUT = ROOT / "output"


def _pick(name: str, primary: Path, *fallbacks: Path) -> Path:
    """First existing candidate; else the primary path so the error names the
    location the file is SUPPOSED to live in, not the last place we looked."""
    p = primary / name
    if p.exists():
        return p
    for d in fallbacks:
        q = d / name
        if q.exists():
            return q
    return p


# --------------------------------------------------------------------------
# input lookups
# --------------------------------------------------------------------------
def raw(name: str) -> Path:
    """A plant source workbook / csv / pdf, verbatim as the plant supplied it."""
    return _pick(name, RAW, DATA_ROOT)


def input_derived(name: str) -> Path:
    """Frozen mined master. NOT interchangeable with `wh_derived` -- see module docstring."""
    return _pick(name, INPUT_DERIVED)


def cycletime(name: str) -> Path:
    return _pick(name, CYCLETIME, DATA_ROOT / "cycletime")


def opening_gt_manual(name: str) -> Path:
    return _pick(name, OPENING_GT_MANUAL, DATA_ROOT / "gtinvaug")


def btpformat(name: str) -> Path:
    return _pick(name, BTPFORMAT, DATA_ROOT / "btpformat")


# --------------------------------------------------------------------------
# engine artefact lookups
# --------------------------------------------------------------------------
def wh_derived(name: str) -> Path:
    """Engine-written artefact. NOT interchangeable with `input_derived`."""
    return WH_DERIVED / name


def params(name: str) -> Path:
    return WH_PARAMS / name


def latest_params() -> Path:
    """Newest `params_*.json` written by L0. L0 itself no longer runs in the
    shipped pipeline -- the file is a committed input."""
    c = sorted(WH_PARAMS.glob("params_*.json"))
    if not c:
        raise FileNotFoundError(
            f"no params_*.json in {WH_PARAMS} -- L0 output is a required input")
    return c[-1]


def demand(month: str) -> Path:
    return MASTERS / "demand" / f"demand_{month}.parquet"


def opening_gt(month: str) -> Path:
    """Honours PLANNER_OPENING_GT: a bare name resolves inside
    `masters/opening_gt/`, an absolute path is taken as given.

    The exporters read this too, not only the planner -- exporting with a
    different PLANNER_OPENING_GT than the arm was built with silently changes
    the GT_Inventory column of the BTP fulfilment sheets while every other
    figure stays identical.
    """
    ov = os.environ.get("PLANNER_OPENING_GT", "").strip()
    d = MASTERS / "opening_gt"
    if ov:
        p = Path(ov)
        return p if p.is_absolute() else d / ov
    return d / f"opening_gt_{month}.parquet"


def press_list(month: str) -> Path:
    return MASTERS / f"press_list_{month}.json"
