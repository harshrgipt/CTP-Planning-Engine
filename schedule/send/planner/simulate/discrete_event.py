"""SimPy-based discrete-event simulator for the deterministic schedule.

Not a full re-plan — a stochastic *evaluation* of the planned schedule under
sampled cycle/setup times. Reports KPI CIs across N replications.
"""
from __future__ import annotations

import json
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import polars as pl
import simpy  # type: ignore

from planner.runs.logger import log
from planner.simulate.dist import LogNormal, fit_cycle_dists, fit_setup_dists


@dataclass
class RepKPI:
    demand_qty: float
    scheduled_qty: float
    actual_completed_qty: float
    demand_fulfillment_rate: float
    changeover_count: int
    machine_util_pct: float
    curing_wait_p95_s: float
    span_s: float


def _reserialize_dist(d: dict) -> LogNormal:
    return LogNormal(**d)


def _run_one_replication(args: tuple[dict, dict, dict, int]) -> dict:
    build_records, cure_records, dists, seed = args
    rng = np.random.default_rng(seed)
    plant_cycle = {k: _reserialize_dist(v) for k, v in dists.get("cycle", {}).items()}
    setup_by_pm = {tuple(k.split("|", 1)): _reserialize_dist(v)
                   for k, v in dists.get("setup", {}).items()}

    env = simpy.Environment()
    tbm: dict[str, simpy.Resource] = {}
    press: dict[str, simpy.Resource] = {}

    tyre_built = []            # (ts_s, plant, gt_code) — supply
    tyre_cured = []            # (ts_s, plant, gt_code, wait_s)

    # Convert timestamps to seconds from schedule start.
    if not build_records:
        return {"seed": seed, "empty": True}
    start_epoch = min(r["start_ts_epoch"] for r in build_records)

    def build_lot(record):
        plant = record["plant"]
        machine = record["machine"]
        gt_code = record["gt_code"]
        qty = int(record["qty"])
        yield env.timeout(max(0, record["start_ts_epoch"] - start_epoch - env.now))
        with tbm.setdefault(machine, simpy.Resource(env, capacity=1)).request() as req:
            yield req
            setup_dist = setup_by_pm.get((plant, machine))
            setup_s = float(setup_dist.sample(rng, 1)[0]) if setup_dist else 900.0
            yield env.timeout(setup_s)
            for i in range(qty):
                # Per-tyre cycle time (build cycle_s constant here for TBM)
                cycle_s = 45.0 if plant == "PCR" else 90.0
                yield env.timeout(cycle_s)
                tyre_built.append((env.now, plant, gt_code))

    def cure_lot(record):
        plant = record["plant"]
        press_id = record["press"]
        gt_code = record["gt_code"]
        # Wait for the tyre to be built — model FIFO by looping until match.
        yield env.timeout(max(0, record["start_ts_epoch"] - start_epoch - env.now))
        # Find earliest matching tyre supply
        idx = None
        for i, (ts, p, g) in enumerate(tyre_built):
            if p == plant and g == gt_code:
                idx = i
                break
        if idx is None:
            # No supply → skip (shortfall)
            return
        built_ts = tyre_built.pop(idx)[0]
        with press.setdefault(press_id, simpy.Resource(env, capacity=1)).request() as req:
            yield req
            dist = plant_cycle.get(plant)
            cure_s = float(dist.sample(rng, 1)[0]) if dist else 1800.0
            wait_s = env.now - built_ts
            yield env.timeout(cure_s)
            tyre_cured.append((env.now, plant, gt_code, wait_s))

    for r in build_records:
        env.process(build_lot(r))
    for r in cure_records:
        env.process(cure_lot(r))

    env.run()

    demand = float(sum(int(r["qty"]) for r in build_records))
    scheduled = demand
    completed = float(len(tyre_cured))
    fulfillment = completed / demand if demand else 0.0
    # Changeovers over build records
    build_records.sort(key=lambda r: (r["machine"], r["start_ts_epoch"]))
    chg = 0
    prev = {}
    for r in build_records:
        p = prev.get(r["machine"])
        if p is not None and p != r["gt_code"]:
            chg += 1
        prev[r["machine"]] = r["gt_code"]
    waits = np.array([w for _, _, _, w in tyre_cured]) if tyre_cured else np.array([0.0])
    wait_p95 = float(np.quantile(waits, 0.95))
    span_s = env.now
    util = 0.0
    if span_s > 0 and tbm:
        # Approximation: sum of build events × cycle_s divided by (span × n_machines)
        occupied = sum(int(r["qty"]) * (45 if r["plant"] == "PCR" else 90)
                       for r in build_records)
        util = 100.0 * occupied / (span_s * max(len(tbm), 1))

    return asdict(RepKPI(
        demand_qty=demand,
        scheduled_qty=scheduled,
        actual_completed_qty=completed,
        demand_fulfillment_rate=fulfillment,
        changeover_count=chg,
        machine_util_pct=util,
        curing_wait_p95_s=wait_p95,
        span_s=span_s,
    )) | {"seed": seed}


def run_replications(
    build_df: pl.DataFrame,
    cure_df: pl.DataFrame,
    learn_dir: Path,
    out_dir: Path,
    *,
    n_reps: int = 200,
    seed_base: int = 42,
    workers: int | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    cycle_dists = fit_cycle_dists(learn_dir)
    setup_dists = fit_setup_dists(learn_dir)
    dists = {
        "cycle": {k: v.to_dict() for k, v in cycle_dists.items()},
        "setup": {f"{p}|{m}": v.to_dict() for (p, m), v in setup_dists.items()},
    }

    build_records = build_df.select([
        "lot_id", "plant", "gt_code", "machine",
        (pl.col("start_ts").cast(pl.Int64) // 1_000_000_000).alias("start_ts_epoch"),
        pl.col("qty").cast(pl.Int64),
    ]).to_dicts()
    cure_records = cure_df.select([
        "lot_id", "plant", "gt_code", "press",
        (pl.col("start_ts").cast(pl.Int64) // 1_000_000_000).alias("start_ts_epoch"),
    ]).to_dicts() if cure_df.height else []

    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    log.info("sim.start", n_reps=n_reps, workers=workers,
             builds=len(build_records), cures=len(cure_records))

    tasks = [(build_records, cure_records, dists, seed_base + i) for i in range(n_reps)]
    results: list[dict] = []
    # Chunk to limit memory: batches of 50.
    batch = 50
    for start in range(0, len(tasks), batch):
        chunk = tasks[start:start + batch]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed(pool.submit(_run_one_replication, t) for t in chunk):
                results.append(fut.result())
        log.info("sim.batch", done=len(results), of=n_reps)

    # Aggregate KPIs
    numeric_keys = ["demand_fulfillment_rate", "changeover_count", "machine_util_pct",
                    "curing_wait_p95_s", "span_s", "actual_completed_qty"]
    agg = {}
    for k in numeric_keys:
        vals = np.array([r[k] for r in results if k in r], dtype=float)
        if vals.size == 0:
            continue
        agg[k] = {
            "mean": float(vals.mean()),
            "p5": float(np.quantile(vals, 0.05)),
            "p95": float(np.quantile(vals, 0.95)),
            "ci_half_width_95": 1.96 * float(vals.std(ddof=1)) / math.sqrt(vals.size),
        }
    out_json = out_dir / "sim_kpis.json"
    out_json.write_text(json.dumps({"n_reps": n_reps, "kpis": agg,
                                     "raw": results[:5]}, indent=2))
    log.info("sim.done", n_reps=n_reps, path=str(out_json))
    return out_json
