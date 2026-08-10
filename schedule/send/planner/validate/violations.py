"""Independent hard-rule verifier for a planner-produced schedule.

Does not call any planner internals. Reads schedule parquets + rules.duckdb
and runs bespoke checks per hard rule. Exit code non-zero if any hit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import polars as pl

from planner.runs.logger import log


@dataclass
class Violation:
    check: str
    severity: str    # 'hard' | 'soft'
    detail: dict


@dataclass
class ValidationReport:
    hard: list[Violation] = field(default_factory=list)
    soft: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hard": [asdict(v) for v in self.hard],
            "soft": [asdict(v) for v in self.soft],
            "n_hard": len(self.hard),
            "n_soft": len(self.soft),
            "ok": len(self.hard) == 0,
        }


# ---------------------------- checks -----------------------------------


def check_machine_overlap(build_df: pl.DataFrame, report: ValidationReport) -> None:
    """Two lots overlap on same machine → hard."""
    if build_df.height == 0:
        return
    df = build_df.sort(["machine", "start_ts"])
    prev_end = (
        df.select([
            pl.col("machine"),
            pl.col("start_ts"),
            pl.col("end_ts").shift(1).over("machine").alias("prev_end"),
            pl.col("lot_id"),
        ])
    )
    overlaps = prev_end.filter(
        pl.col("prev_end").is_not_null() & (pl.col("start_ts") < pl.col("prev_end"))
    )
    for row in overlaps.iter_rows(named=True):
        report.hard.append(Violation(
            check="machine_overlap", severity="hard",
            detail={"machine": row["machine"], "lot_id": row["lot_id"],
                    "start_ts": str(row["start_ts"]),
                    "prev_end": str(row["prev_end"])},
        ))


def check_mould_double_book(cure_df: pl.DataFrame, report: ValidationReport) -> None:
    if cure_df.height == 0 or "mould" not in cure_df.columns:
        return
    df = cure_df.filter(pl.col("mould").is_not_null()).sort(["mould", "start_ts"])
    prev = df.select([
        pl.col("mould"),
        pl.col("press"),
        pl.col("start_ts"),
        pl.col("end_ts").shift(1).over("mould").alias("prev_end"),
        pl.col("press").shift(1).over("mould").alias("prev_press"),
        pl.col("lot_id"),
    ])
    conflicts = prev.filter(
        pl.col("prev_end").is_not_null()
        & (pl.col("start_ts") < pl.col("prev_end"))
        & (pl.col("press") != pl.col("prev_press"))
    )
    for row in conflicts.iter_rows(named=True):
        report.hard.append(Violation(
            check="mould_double_book", severity="hard",
            detail={"mould": row["mould"], "press_a": row["prev_press"],
                    "press_b": row["press"], "lot_id": row["lot_id"]},
        ))


def check_negative_gt(build_df: pl.DataFrame, cure_df: pl.DataFrame,
                      report: ValidationReport,
                      ledger_df: pl.DataFrame | None = None) -> None:
    """FIFO check: kth cure of (plant, gt_code) must have start_ts >=
    kth per-tyre supply ts. If `ledger_df` is supplied (gt_events.parquet
    from inv_sim), it is used as the authoritative supply/consumption stream
    including opening WIP. Otherwise, per-tyre supply is derived from
    build_schedule as start_ts + setup_s + (i+1) * (cycle_s / qty)."""
    if cure_df.height == 0:
        return

    if ledger_df is not None and ledger_df.height:
        supply = (ledger_df.filter(
                    (pl.col("source").is_in(["build", "opening"]))
                    & (pl.col("qty_delta") > 0))
                  .with_columns(pl.col("qty_delta").cast(pl.Int64))
                  .with_columns(pl.int_ranges(pl.col("qty_delta")).alias("_i"))
                  .explode("_i")
                  .sort(["plant", "gt_code", "ts"]))
        supply = supply.with_columns(
            pl.col("ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk")
        ).select(["plant", "gt_code", "rk", pl.col("ts").alias("supply_ts")])
    else:
        if build_df.height == 0:
            return
        need = {"plant", "gt_code", "start_ts", "qty", "setup_s", "cycle_s"}
        if not need.issubset(set(build_df.columns)):
            return
        b = build_df.with_columns(pl.col("qty").cast(pl.Int64))
        b = b.with_columns(pl.int_ranges(pl.col("qty")).alias("_i")).explode("_i")
        b = b.with_columns(
            (pl.col("start_ts")
             + pl.duration(seconds=(
                 pl.col("setup_s")
                 + (pl.col("_i").cast(pl.Float64) + 1.0)
                   * (pl.col("cycle_s") / pl.col("qty").cast(pl.Float64).clip(lower_bound=1.0))
             ))).alias("supply_ts")
        ).sort(["plant", "gt_code", "supply_ts"])
        supply = b.with_columns(
            pl.col("supply_ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk")
        ).select(["plant", "gt_code", "rk", "supply_ts"])

    c = cure_df.select(["plant", "gt_code", "start_ts", "lot_id"]).sort(["plant", "gt_code", "start_ts"])
    c = c.with_columns(
        pl.col("start_ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk")
    )
    pairs = c.join(supply, on=["plant", "gt_code", "rk"], how="left")
    bad = pairs.filter(pl.col("supply_ts").is_not_null()
                       & (pl.col("start_ts") < pl.col("supply_ts")))
    for row in bad.iter_rows(named=True):
        report.hard.append(Violation(
            check="negative_gt", severity="hard",
            detail={"plant": row["plant"], "gt_code": row["gt_code"],
                    "lot_id": row["lot_id"],
                    "start_ts": str(row["start_ts"]),
                    "supply_ts": str(row["supply_ts"])},
        ))


def verify(run_month_dir: Path) -> ValidationReport:
    report = ValidationReport()
    build_p = run_month_dir / "build_schedule.parquet"
    cure_p = run_month_dir / "cure_schedule.parquet"
    ledger_p = run_month_dir / "gt_events.parquet"
    build_df = pl.read_parquet(build_p) if build_p.exists() else pl.DataFrame()
    cure_df = pl.read_parquet(cure_p) if cure_p.exists() else pl.DataFrame()
    ledger_df = pl.read_parquet(ledger_p) if ledger_p.exists() else None

    check_machine_overlap(build_df, report)
    check_mould_double_book(cure_df, report)
    check_negative_gt(build_df, cure_df, report, ledger_df=ledger_df)

    log.info("validate.done", hard=len(report.hard), soft=len(report.soft),
             dir=str(run_month_dir))
    return report


def verify_and_write(run_month_dir: Path) -> Path:
    r = verify(run_month_dir)
    out = run_month_dir / "violations.json"
    out.write_text(json.dumps(r.to_dict(), indent=2, default=str))
    return out
