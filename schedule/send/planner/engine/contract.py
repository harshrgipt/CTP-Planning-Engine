"""PHASE 0 -- input contract, and the determinism spine for the whole engine.

The engine's promise is: same demand + same inventory + same masters ==>
BYTE-IDENTICAL plan. That does not happen by accident. Every source of
nondeterminism is closed here or in the phase that owns it:

    RNG                 forbidden in the plan path
    wall clock          forbidden -- all timestamps derive from horizon start
    dict/set iteration  every key sorted before use (`ordered`)
    polars group_by     order is NOT stable -- always .sort() after
    float accumulation  tyre counts are integers; ties broken on an explicit key
    parallelism         single-threaded in the plan path
    fixed-point loop    fixed iteration count, never a wall-clock budget
    master drift        every input hashed into the run id

INTEGER QUANTITIES ARE A HARD GATE, not a nicety. A fractional quantity makes
`int_ranges(0.5::BIGINT)` an EMPTY range, which becomes a NULL-timestamped
ledger event, which scrambles the FIFO ranks the verifier derives, which
manufactures phantom violations. That chain has cost two separate debugging
cycles (1,113 and 236 phantom violations). Stop it at the door.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import polars as pl

from planner.runs.logger import log

from planner.config import GT_SHELF_LIFE_H as SHELF_LIFE_H  # hardcoded 72 h


def ordered(it: Iterable) -> list:
    """Sorted materialisation. Use for EVERY dict/set iteration in the engine."""
    return sorted(it)


def stable_sort(df: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    """Polars group_by does not guarantee row order; always re-sort after one."""
    return df.sort(by, maintain_order=True)


def digest(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()[:12]


@dataclass
class PlanRequest:
    """A validated, hashable planning request."""
    plan_start: date
    plan_end: date
    demand: pl.DataFrame          # plant, gt_code, due_date, qty (int)
    opening: pl.DataFrame         # plant, gt_code, built_ts  (one row per tyre)
    horizon_days: int
    input_hash: str
    rejected: list[dict] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def start_ts(self) -> datetime:
        return datetime.combine(self.plan_start, datetime.min.time())

    def demand_total(self) -> float:
        return float(self.demand["qty"].sum()) if self.demand.height else 0.0

    def to_dict(self) -> dict:
        return {
            "plan_start": str(self.plan_start), "plan_end": str(self.plan_end),
            "horizon_days": self.horizon_days, "input_hash": self.input_hash,
            "demand_rows": self.demand.height,
            "demand_total": int(self.demand_total()),
            "demand_gts": int(self.demand["gt_code"].n_unique()) if self.demand.height else 0,
            "opening_tyres": self.opening.height,
            "opening_gts": int(self.opening["gt_code"].n_unique()) if self.opening.height else 0,
            "rejected": self.rejected, "quarantined": self.quarantined,
            "notes": self.notes,
        }


class ContractError(RuntimeError):
    pass


def _read(p: Path) -> pl.DataFrame:
    if not p.exists():
        raise ContractError(f"input not found: {p}")
    return pl.read_parquet(p) if p.suffix == ".parquet" else pl.read_csv(p)


def load_request(demand_path: Path, opening_path: Path | None,
                 plan_start: date, plan_end: date) -> PlanRequest:
    """PHASE 0. Validate, normalise and hash the user's inputs."""
    rejected: list[dict] = []
    quarantined: list[dict] = []
    notes: list[str] = []
    H = (plan_end - plan_start).days + 1

    # ---- demand ---------------------------------------------------------
    d = _read(Path(demand_path))
    need = {"plant", "gt_code", "qty"}
    if not need.issubset(d.columns):
        raise ContractError(f"demand needs {sorted(need)}, has {sorted(d.columns)}")
    if "due_date" not in d.columns:
        # a monthly total is acceptable -- the engine derives its own daily
        # build profile from the press plan anyway.
        d = d.with_columns(pl.lit(plan_end).alias("due_date"))
        notes.append("demand had no due_date; treated as a month total")
    d = d.with_columns(pl.col("due_date").cast(pl.Date),
                       pl.col("qty").cast(pl.Float64))

    frac = d.filter((pl.col("qty") % 1) != 0)
    if frac.height:
        raise ContractError(
            f"{frac.height} demand rows have fractional qty. Round at source -- "
            "a fractional tyre becomes a NULL-timestamped ledger event and "
            "corrupts every FIFO rank downstream.")
    d = d.with_columns(pl.col("qty").cast(pl.Int64))

    bad = d.filter((pl.col("due_date") < plan_start) | (pl.col("due_date") > plan_end))
    if bad.height:
        for r in bad.head(20).iter_rows(named=True):
            rejected.append({"why": "due_date outside horizon", **{
                k: str(v) for k, v in r.items()}})
        d = d.filter((pl.col("due_date") >= plan_start) & (pl.col("due_date") <= plan_end))

    nonpos = d.filter(pl.col("qty") <= 0)
    if nonpos.height:
        notes.append(f"dropped {nonpos.height} demand rows with qty <= 0")
        d = d.filter(pl.col("qty") > 0)

    d = stable_sort(d.select(["plant", "gt_code", "due_date", "qty"]),
                    ["plant", "gt_code", "due_date"])

    # ---- opening inventory ----------------------------------------------
    if opening_path is None:
        o = pl.DataFrame({"plant": [], "gt_code": [], "built_ts": []},
                         schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                                 "built_ts": pl.Datetime})
        notes.append("no opening inventory supplied; starting from empty racks")
    else:
        o = _read(Path(opening_path))
        if "built_ts" not in o.columns:
            # aggregate form: expand to one row per tyre, aged at the horizon
            # open. Ageing cannot be modelled without timestamps, so say so.
            if "qty" not in o.columns:
                raise ContractError("opening needs built_ts (per tyre) or qty")
            notes.append("opening had no built_ts; assumed fresh at horizon open "
                         "-- FIFO ageing of carry-over stock is NOT modelled")
            o = o.with_columns(pl.col("qty").cast(pl.Int64))
            o = (o.with_columns(pl.int_ranges(pl.col("qty")).alias("_i"))
                   .explode("_i")
                   .with_columns(pl.lit(datetime.combine(plan_start,
                                                         datetime.min.time()))
                                 .alias("built_ts")))
        o = o.with_columns(pl.col("built_ts").cast(pl.Datetime))
        o = o.select(["plant", "gt_code", "built_ts"])

        # Quarantine stock already past its shelf life at the horizon open --
        # it is scrap, not inventory, and counting it invents capacity.
        t0 = datetime.combine(plan_start, datetime.min.time())
        age = ((pl.lit(t0) - pl.col("built_ts")).dt.total_seconds() / 3600.0)
        stale = o.filter(age > SHELF_LIFE_H)
        if stale.height:
            for r in (stale.group_by(["plant", "gt_code"]).len()
                           .sort("len", descending=True).head(20)
                           .iter_rows(named=True)):
                quarantined.append({"why": f"opening age > {SHELF_LIFE_H:.0f}h",
                                    "plant": r["plant"], "gt_code": r["gt_code"],
                                    "tyres": int(r["len"])})
            o = o.filter(age <= SHELF_LIFE_H)
        o = stable_sort(o, ["plant", "gt_code", "built_ts"])

    req = PlanRequest(
        plan_start=plan_start, plan_end=plan_end, demand=d, opening=o,
        horizon_days=H,
        input_hash=digest(
            d.to_dicts() if d.height <= 20000 else d.select(
                pl.col("qty").sum(), pl.len()).to_dicts(),
            o.select(pl.len()).to_dicts(),
            str(plan_start), str(plan_end)),
        rejected=rejected, quarantined=quarantined, notes=notes)
    log.info("engine.contract", **{k: v for k, v in req.to_dict().items()
                                   if not isinstance(v, list)})
    return req
