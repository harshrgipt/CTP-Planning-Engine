"""BOM xlsx ingest: normalize hierarchical parent->child rows into an edge table
and derive SKU->GT map. Uses polars.read_excel via openpyxl."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.runs.logger import log


BOM_COLS = [
    "Super_parent", "Equipment", "grand_parent",
    "Parent", "Parent_qty", "Parent_unit",
    "child", "child_quantity", "child_Unit", "child_description",
]

RENAME = {
    "Super_parent": "super_parent_sku",
    "Equipment":     "equipment",
    "grand_parent":  "grand_parent",
    "Parent":        "parent",
    "Parent_qty":    "parent_qty",
    "Parent_unit":   "parent_unit",
    "child":         "child",
    "child_quantity": "child_qty",
    "child_Unit":    "child_unit",
    "child_description": "child_desc",
}


def _read_bom_xlsx(path: Path, plant: str) -> pl.DataFrame:
    if not path.exists():
        log.warning("bom.missing", path=str(path))
        return pl.DataFrame()
    df = pl.read_excel(path)
    # Only keep expected columns; some sheets may add extras.
    keep = [c for c in BOM_COLS if c in df.columns]
    df = df.select(keep).rename({k: RENAME[k] for k in keep})
    df = df.with_columns(pl.lit(plant).alias("plant"))
    # Cast qty numeric where possible
    for c in ("parent_qty", "child_qty"):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
    return df


def build_bom_edges() -> Path:
    root = CONFIG.paths.raw_bom
    pcr = _read_bom_xlsx(root / "jkt_bom_pcr 23.xlsx", "PCR")
    tbr = _read_bom_xlsx(root / "jkt_bom_tbr 5.xlsx", "TBR")
    df = pl.concat([f for f in (pcr, tbr) if f.height > 0])
    out_dir = CONFIG.paths.warehouse / "bom"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "bom_edges.parquet"
    df.write_parquet(out, compression="zstd")
    log.info("bom.edges.written", path=str(out), rows=df.height)
    return out


def build_gt_map() -> Path:
    """Extract SKU -> GT code mapping from edges where child_description == 'Green Tyres'."""
    edges_path = CONFIG.paths.warehouse / "bom" / "bom_edges.parquet"
    if not edges_path.exists():
        build_bom_edges()
    df = pl.read_parquet(edges_path)
    gt = (
        df.filter(pl.col("child_desc").str.strip_chars().str.to_lowercase() == "green tyres")
          .select(["plant", "super_parent_sku", "child"])
          .rename({"child": "gt_code"})
          .unique()
    )
    out = CONFIG.paths.warehouse / "bom" / "bom_gt_map.parquet"
    gt.write_parquet(out, compression="zstd")
    log.info("bom.gt_map.written", path=str(out), rows=gt.height)
    return out


def run() -> None:
    build_bom_edges()
    build_gt_map()
