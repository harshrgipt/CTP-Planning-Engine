"""PROGRAMMATIC ENTRY POINT FOR A FRONTEND.  One call = one planned month.

    from planner.api import plan_month, get_holidays, set_holidays, list_months

    res = plan_month("2026-08", holidays=["2026-08-15"])
    res["ok"]            -> True / False
    res["kpi"]["PCR"]    -> {"built": 400379, "in_month": 401657, ...}
    res["pack_dir"]      -> "output/2026-08_pack"

WHY THIS FILE EXISTS
  `main.py` is an argparse CLI: it parses `sys.argv`, prints banners and calls
  `sys.exit`. A web backend cannot use any of that -- it needs a function that
  takes values, returns a dict and raises instead of exiting. This module is that
  adapter and nothing else. It re-uses `main.py`'s own pipeline (`_plan_core`,
  `_export`) rather than restating the stage list, so the CLI and the frontend
  can never drift into planning two different things.

THE HOLIDAY CONTRACT, WHICH IS THE POINT OF THIS FILE
  The frontend sends holidays; this module writes them to
  `masters/holidays_<month>.json` and plans. That file is the ONE source of truth
  that `planner/cmbc/holiday.py` reads, so what the user saw in the browser is
  literally what the engine planned against.

  A holiday is a PLANT-DAY, 07:00 -> 07:00 (the plant day does not start at
  midnight). "2026-08-15" closes [15 Aug 07:00, 16 Aug 07:00): no press cures and
  no machine builds. Work is NOT deleted -- a cure campaign that meets the
  closure pauses and resumes after it.

  `PLANNER_HOLIDAYS` (env) OVERRIDES the file inside the engine. This module
  therefore refuses to run if that variable is set, rather than silently planning
  a different calendar than the one the frontend just saved.

WHAT THE CALLER MUST KNOW
  * A month needs its demand + opening GT ingested first (`main.py masters`), and
    a partition stamped for THAT month. `plan_month` reports which is missing
    instead of failing deep inside a layer.
  * Grade on `built`, not `in_month`. `in_month` counts opening stock fed from
    the previous month and is tail-sensitive, so it moves without a tyre being
    made. Both are returned; the frontend should show both.
  * This runs the real pipeline (~30-60 s). Call it from a worker, not from a
    request handler.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import polars as pl

from planner import paths

ROOT = Path(__file__).resolve().parent.parent
PLANTS = ("PCR", "TBR")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PlanError(RuntimeError):
    """Anything the frontend should show the user verbatim."""


# ---------------------------------------------------------------- helpers ----
def _check_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise PlanError(f"month must look like '2026-08', got {month!r}")
    return month


def _norm_dates(v: Any, month: str) -> list[str]:
    """Accept str | date | datetime | iterable of those. Return sorted ISO strs."""
    if v is None:
        return []
    if isinstance(v, (str, date, datetime)):
        v = [v]
    if not isinstance(v, Iterable):
        raise PlanError(f"holidays must be a list of dates, got {type(v).__name__}")
    out: set[str] = set()
    for x in v:
        if isinstance(x, datetime):
            x = x.date()
        if isinstance(x, date):
            s = x.isoformat()
        elif isinstance(x, str):
            s = x.strip()
            try:
                date.fromisoformat(s)
            except ValueError:
                raise PlanError(f"bad holiday date {x!r} -- use 'YYYY-MM-DD'") from None
        else:
            raise PlanError(f"bad holiday value {x!r} -- use 'YYYY-MM-DD'")
        if not s.startswith(month):
            raise PlanError(f"holiday {s} is not inside month {month}")
        out.add(s)
    return sorted(out)


def _norm_holidays(holidays: Any, month: str) -> dict[str, list[str]]:
    """-> {"all": [...], "PCR": [...], "TBR": [...]}, empty lists dropped."""
    if holidays is None:
        return {}
    if isinstance(holidays, dict):
        spec: dict[str, list[str]] = {}
        for k, v in holidays.items():
            key = str(k).strip().upper()
            key = "all" if key in ("ALL", "BOTH", "") else key
            if key not in ("all", *PLANTS):
                raise PlanError(f"unknown holiday key {k!r} -- use 'all', 'PCR' or 'TBR'")
            d = _norm_dates(v, month)
            if d:
                spec[key] = d
        return spec
    d = _norm_dates(holidays, month)
    return {"all": d} if d else {}


def _guard_env() -> None:
    if os.environ.get("PLANNER_HOLIDAYS") is not None:
        raise PlanError(
            "PLANNER_HOLIDAYS is set in the environment and OVERRIDES the saved "
            "calendar -- the plan would not match what was entered. Unset it.")


# ------------------------------------------------------------- holidays ------
def get_holidays(month: str) -> dict[str, Any]:
    """What is currently armed for this month. Safe to call any time."""
    month = _check_month(month)
    f = paths.holidays(month)
    if not f.exists():
        return {"month": month, "armed": False, "holidays": {}, "file": str(f)}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:                                      # noqa: BLE001
        raise PlanError(f"{f.name} is not valid JSON: {exc}") from None
    if isinstance(raw, list):
        spec = {"all": [str(x) for x in raw]}
    else:
        spec = {k: [str(x) for x in v] for k, v in raw.items()
                if not str(k).startswith("_") and isinstance(v, list)}
    return {"month": month, "armed": bool(spec), "holidays": spec, "file": str(f)}


def set_holidays(month: str, holidays: Any) -> dict[str, Any]:
    """Save the month's closure calendar. Pass None/[] to clear it.

    This is what the frontend calls when the user edits the calendar. It only
    writes the file -- it does not plan. Call `plan_month` after.
    """
    month = _check_month(month)
    spec = _norm_holidays(holidays, month)
    f = paths.holidays(month)
    f.parent.mkdir(parents=True, exist_ok=True)
    if not spec:
        f.unlink(missing_ok=True)
        return {"month": month, "armed": False, "holidays": {}, "file": str(f)}
    body = {"_README": "Plant closure calendar (rule G3). A holiday is a "
                       "PLANT-DAY 07:00->07:00. Written by planner.api.",
            **spec}
    f.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return {"month": month, "armed": True, "holidays": spec, "file": str(f)}


# ------------------------------------------------------------- readiness -----
def month_status(month: str) -> dict[str, Any]:
    """Can this month be planned? Report every blocker at once, not the first."""
    month = _check_month(month)
    wh = ROOT / "warehouse" / "derived"
    blockers: list[str] = []

    if not (wh / f"net_requirement_{month}.parquet").exists() \
            and not (ROOT / "masters" / "demand" / f"demand_{month}.parquet").exists():
        blockers.append(f"demand for {month} not ingested "
                        f"(run: main.py masters --month {month})")

    part = paths.input_derived("gt_machine_partition.parquet")
    if not part.exists():
        blockers.append("gt_machine_partition.parquet is missing")
    else:
        try:
            stamp = pl.read_parquet(part)["month"].unique().to_list()
            if stamp != [month]:
                blockers.append(
                    f"partition is stamped {stamp} but you asked for {month}. "
                    f"It is sized against ONE month's demand and calendar hours; "
                    f"L7 refuses a foreign stamp. Rebuild it for {month}.")
        except Exception as exc:                                  # noqa: BLE001
            blockers.append(f"partition unreadable: {exc}")

    if os.environ.get("PLANNER_HOLIDAYS") is not None:
        blockers.append("PLANNER_HOLIDAYS is set and overrides the saved calendar")

    return {"month": month, "ready": not blockers, "blockers": blockers,
            **{k: v for k, v in get_holidays(month).items() if k != "month"}}


# ------------------------------------------------------------------ KPI ------
def read_kpi(run: str, month: str) -> dict[str, Any]:
    """Per-plant KPIs from a finished run. BUILT is the production number."""
    month = _check_month(month)
    d = ROOT / "runs" / run
    if not (d / "cure_campaigns_reconciled.parquet").exists():
        raise PlanError(f"runs/{run} has no reconciled campaigns -- did it finish?")
    nr = pl.read_parquet(ROOT / "warehouse" / "derived"
                         / f"net_requirement_{month}.parquet")
    dem = {r["plant"]: float(r["demand"]) for r in
           nr.group_by("plant").agg(pl.col("demand").sum()).to_dicts()}
    rec = pl.read_parquet(d / "cure_campaigns_reconciled.parquet")
    bs = pl.read_parquet(d / "build_schedule.parquet").filter(
        pl.col("machine") != "OPENING_STOCK")
    out: dict[str, Any] = {}
    for p in PLANTS:
        r = rec.filter(pl.col("plant") == p)
        b = bs.filter(pl.col("plant") == p)
        if not r.height:
            continue
        built = float(b["qty"].sum())
        fed = float(r["qty_fed_in_month"].sum())
        demand = dem.get(p, 0.0) or 1.0
        out[p] = {
            "demand": int(demand),
            "built": int(built),                       # <- the production number
            "built_pct": round(100 * built / demand, 2),
            "in_month": int(fed),                      # <- tail-sensitive
            "in_month_pct": round(100 * fed / demand, 2),
            "tail": int(float(r["qty"].sum()) - fed),
            "starved": int(float(r["qty_unfed"].sum())),
            "gt_wait_max_h": round(float(b["wait_h"].max()), 1),
        }
    inv = d / "l11_invariants.parquet"
    if inv.exists():
        i = pl.read_parquet(inv)
        if "status" in i.columns:
            out["l11"] = {"pass": int(i.filter(pl.col("status") == "PASS").height),
                          "total": int(i.height)}
    return out


# ------------------------------------------------------------------ plan -----
def plan_month(month: str,
               holidays: Any = None,
               *,
               run: str | None = None,
               out: str | None = None,
               opening_gt: str | None = None,
               overrides: dict[str, str] | None = None,
               save_holidays: bool = True,
               btp_only: bool = False,
               force_partition: bool = False,
               strict: bool = False,
               verbose: bool = False) -> dict[str, Any]:
    """Plan one month end to end and return a JSON-serialisable result.

    holidays  "2026-08-15" | ["2026-08-15", ...] | {"PCR": [...], "TBR": [...]}
              None leaves whatever is already saved; [] clears it.
    run/out   default to `plan_<month>` and `output/<month>_pack`.
    overrides extra PLANNER_* settings, applied to this call only.

    Raises PlanError with a message meant for the user. Never calls sys.exit.
    """
    month = _check_month(month)
    _guard_env()

    if holidays is not None and save_holidays:
        set_holidays(month, holidays)

    st = month_status(month)
    if not st["ready"]:
        raise PlanError("cannot plan " + month + ":\n  - " + "\n  - ".join(st["blockers"]))

    run = run or f"plan_{month}"
    out_dir = Path(out) if out else ROOT / "output" / f"{month}_pack"

    import main as _cli                       # imported here: it touches argv/env
    env = _cli._env([f"{k}={v}" for k, v in (overrides or {}).items()], opening_gt)
    env.pop("PLANNER_HOLIDAYS", None)         # the file is the source of truth

    # EVERY attribute main.py's `planlike`/`common` parsers define. Miss one and
    # the failure lands deep inside `_export` AFTER the whole pipeline has run
    # (~60 s wasted) -- which is exactly what happened the first time. If the CLI
    # grows an argument, add it here too.
    a = SimpleNamespace(month=month, run=run, out=str(out_dir),
                        opening_gt=opening_gt, verbose=verbose,
                        btp_only=btp_only, force_partition=force_partition,
                        strict=strict, set=[])
    started = datetime.now()
    try:
        _cli._plan_core(a, env)
        _cli._export(a, env, run, out_dir, quiet=not verbose)
    except SystemExit as exc:                 # a layer failed its own gate
        raise PlanError(f"pipeline stopped at exit({exc.code}) -- see "
                        f"runs/{run}/log_*.txt for the failing step") from None

    return {
        "ok": True,
        "month": month,
        "run": run,
        "run_dir": str(ROOT / "runs" / run),
        "pack_dir": str(out_dir),
        "holidays": get_holidays(month)["holidays"],
        "kpi": read_kpi(run, month),
        "seconds": round((datetime.now() - started).total_seconds(), 1),
    }


def list_months() -> list[str]:
    """Months that have demand ingested, newest first."""
    wh = ROOT / "warehouse" / "derived"
    ms = {f.stem.replace("net_requirement_", "")
          for f in wh.glob("net_requirement_*.parquet")}
    return sorted((m for m in ms if _MONTH_RE.match(m)), reverse=True)
