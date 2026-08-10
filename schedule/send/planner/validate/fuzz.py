"""Adversarial-schedule generators to prove the verifier catches every violation."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from planner.validate.violations import verify


def _base_build_row(i: int, machine: str, gt: str, start: datetime, qty: int = 10) -> dict:
    return {
        "lot_id": f"L{i}",
        "plant": "PCR",
        "gt_code": gt,
        "stage": "build_s2",
        "machine": machine,
        "start_ts": start,
        "end_ts": start + timedelta(minutes=30 * qty),
        "qty": qty,
        "setup_s": 0.0,
        "cycle_s": 30.0 * qty,
    }


def _base_cure_row(i: int, plant: str, gt: str, press: str, mould: str | None,
                   start: datetime) -> dict:
    return {
        "cure_id": i,
        "lot_id": f"C{i}",
        "plant": plant,
        "gt_code": gt,
        "press": press,
        "mould": mould,
        "start_ts": start,
        "end_ts": start + timedelta(minutes=30),
        "cycle_s": 1800.0,
        "source_build_lot": None,
    }


def fuzz_machine_overlap(dir: Path) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    t = datetime(2026, 1, 1, 0, 0, 0)
    # Two lots overlap on machine M1
    b = pl.DataFrame([
        _base_build_row(1, "M1", "GT_A", t, qty=5),
        _base_build_row(2, "M1", "GT_B", t + timedelta(minutes=60), qty=5),  # overlap
    ])
    b.write_parquet(dir / "build_schedule.parquet", compression="zstd")
    return dir


def fuzz_mould_double_book(dir: Path) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    t = datetime(2026, 1, 1, 0, 0, 0)
    b = pl.DataFrame([
        _base_build_row(1, "M1", "GT_A", t, qty=1),
        _base_build_row(2, "M2", "GT_A", t, qty=1),
    ])
    c = pl.DataFrame([
        _base_cure_row(1, "PCR", "GT_A", "P1", "MOULD_X", t + timedelta(minutes=45)),
        _base_cure_row(2, "PCR", "GT_A", "P2", "MOULD_X", t + timedelta(minutes=50)),  # overlap
    ])
    b.write_parquet(dir / "build_schedule.parquet", compression="zstd")
    c.write_parquet(dir / "cure_schedule.parquet", compression="zstd")
    return dir


def fuzz_negative_gt(dir: Path) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    t = datetime(2026, 1, 1, 0, 0, 0)
    # Build finishes at t+30min, cure starts at t (before build ends)
    b = pl.DataFrame([_base_build_row(1, "M1", "GT_A", t, qty=1)])
    c = pl.DataFrame([_base_cure_row(1, "PCR", "GT_A", "P1", None, t)])  # too early
    b.write_parquet(dir / "build_schedule.parquet", compression="zstd")
    c.write_parquet(dir / "cure_schedule.parquet", compression="zstd")
    return dir


def run_all_fuzz(root: Path) -> dict[str, bool]:
    """Run each fuzz case, verify it is caught. Returns {case: caught}."""
    results = {}
    for name, gen, expected in [
        ("machine_overlap",     fuzz_machine_overlap,   "machine_overlap"),
        ("mould_double_book",   fuzz_mould_double_book, "mould_double_book"),
        ("negative_gt",         fuzz_negative_gt,       "negative_gt"),
    ]:
        d = gen(root / name)
        r = verify(d)
        caught = any(v.check == expected for v in r.hard)
        results[name] = caught
    return results
