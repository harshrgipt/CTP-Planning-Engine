"""Pluggable master-data loaders. Every master is optional today; loaders
return empty typed frames + a warning when a file is missing. Once the plant
drops real files into masters/, the same signature returns real data with no
downstream refactor."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.runs.logger import log


EMPTY_SCHEMAS: dict[str, dict[str, pl.PolarsDataType]] = {
    "demand":                 {"sku": pl.Utf8, "due_date": pl.Date, "qty": pl.Float64, "priority": pl.Int32},
    "routing":                {"sku": pl.Utf8, "stage1_group": pl.Utf8, "stage2_group": pl.Utf8, "press_group": pl.Utf8},
    "machine_master":         {"machine_id": pl.Utf8, "type": pl.Utf8, "group": pl.Utf8, "capacity": pl.Float64, "calendar_id": pl.Utf8},
    "allowed_machine_matrix": {"sku": pl.Utf8, "machine_id": pl.Utf8, "allowed": pl.Boolean},
    "cycle_time":             {"sku": pl.Utf8, "machine_id": pl.Utf8, "minutes_per_unit": pl.Float64},
    "setup_time":             {"from_sku": pl.Utf8, "to_sku": pl.Utf8, "machine_id": pl.Utf8, "minutes": pl.Float64},
    "calendar":               {"date": pl.Date, "shift": pl.Utf8, "start": pl.Datetime, "end": pl.Datetime, "is_holiday": pl.Boolean},
    "opening_inventory":      {"sku": pl.Utf8, "qty": pl.Float64, "as_of": pl.Date},
    "opening_wip":            {"sku": pl.Utf8, "stage": pl.Utf8, "qty": pl.Float64, "as_of": pl.Date},
    "moq_mpq":                {"sku": pl.Utf8, "moq": pl.Float64, "mpq": pl.Float64},
    "aging_rules":            {"sku": pl.Utf8, "min_hours": pl.Float64, "max_hours": pl.Float64},
}


def load(name: str) -> pl.DataFrame:
    if name not in EMPTY_SCHEMAS:
        raise KeyError(f"unknown master '{name}'")
    for ext in (".parquet", ".csv"):
        p: Path = CONFIG.paths.masters / f"{name}{ext}"
        if p.exists():
            log.info("masters.load", name=name, path=str(p))
            return pl.read_parquet(p) if ext == ".parquet" else pl.read_csv(p)
    schema = EMPTY_SCHEMAS[name]
    log.warning("masters.stub", name=name, path=str(CONFIG.paths.masters / f"{name}.*"))
    return pl.DataFrame(schema=schema)
