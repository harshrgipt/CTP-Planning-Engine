"""SKU construction mapping ingest.

- PCR xlsx: header at row 5 (index 4). Cols: SKU, Size, RimDia, GT Code,
  PLY 1, PLY 2 UP/DOWN, FLIPPER, CAP STRIP, Cycle Time per Tyre (Sec),
  Tyres/Shift/Machine @100%, GT Code Updated, Component Code.
- TBR xlsx: multi-sheet.
    * Sheet5 -> per-SKU slot map
    * Sheet6 -> per-GT slot template
    * Sheet4 -> test SKU list
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.runs.logger import log


PCR_XLSX = "SKU wise construction mapping PCR.xlsx"
TBR_XLSX = "SKU wise construction mapping TBR.xlsx"


PCR_COLS = {
    "Product Code":                 "sku",
    "Size Description":             "size",
    "Rim Dia":                      "rim_dia",
    "GT Code":                      "gt_code",
    "PLY 1":                        "ply1",
    "PLY 2 UP":                     "ply2_up",
    "PLY 2 DOWN":                   "ply2_dn",
    "FLIPPER":                      "flipper",
    "CAP STRIP":                    "cap_strip",
    "Cycle Time":                   "cycle_time_sec",
    "No of Tyres":                  "tyres_per_shift_100",
    "GT Code Updated":              "gt_code_updated",
    "Component Code":               "component_code",
}


def _pcr(path: Path) -> pl.DataFrame:
    if not path.exists():
        log.warning("construction.pcr.missing", path=str(path))
        return pl.DataFrame()
    # Header is on row 5; polars.read_excel supports read_options.
    raw = pl.read_excel(
        path,
        sheet_name="PCR",
        read_options={"header_row": 4},
    )
    # Longest-prefix-first so "GT Code Updated" matches before "GT Code".
    sorted_prefixes = sorted(PCR_COLS.items(), key=lambda kv: -len(kv[0]))
    ren: dict[str, str] = {}
    for col in raw.columns:
        norm = " ".join(str(col).split())
        for prefix, target in sorted_prefixes:
            if norm.startswith(prefix) and target not in ren.values():
                ren[col] = target
                break
    df = raw.rename(ren)
    keep = [c for c in PCR_COLS.values() if c in df.columns]
    df = df.select(keep)
    # Drop non-SKU rows (title/annexure leftovers)
    df = df.filter(pl.col("sku").is_not_null())
    # Numeric casts
    for c in ("rim_dia", "cycle_time_sec", "tyres_per_shift_100"):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
    df = df.with_columns(pl.lit("PCR").alias("plant"))
    return df


TBR_SHEET5_COLS = {
    "Material":         "sku",
    "Matl. Description": "description",
    "Component":        "gt_code",
    "PRE ASSEMBLY":     "pre_assembly",
    "NYLON-1":          "nylon1",
    "NYLON2&3":         "nylon23",
    "GUM STRIP":        "gum_strip",
    "STEEL CHIPPER LEFT":  "chipper_l",
    "STEEL CHIPPER RIGHT": "chipper_r",
    "BODYPLY":          "bodyply",
    "SHOULDER PAD":     "shoulder_pad",
    "APEXED BEAD":      "apexed_bead",
    "BELT-1":           "belt1",
    "BELT-2":           "belt2",
    "BELT EDGE FILLER": "belt_edge_filler",
    "BELT-3":           "belt3",
    "BELT-4":           "belt4",
    "Tread Code":       "tread_code",
}

TBR_SHEET6_COLS = {
    "IIOT Code":        "gt_code",
    "PRE ASSEMBLY":     "pre_assembly",
    "NYLON-1":          "nylon1",
    "NYLON2&3":         "nylon23",
    "GUM STRIP":        "gum_strip",
    "STEEL CHIPPER LEFT":  "chipper_l",
    "STEEL CHIPPER RIGHT": "chipper_r",
    "BODYPLY":          "bodyply",
    "SHOULDER PAD":     "shoulder_pad",
    "APEXED BEAD":      "apexed_bead",
    "BELT-1":           "belt1",
    "BELT-2":           "belt2",
    "BELT EDGE FILLER": "belt_edge_filler",
    "BELT-3":           "belt3",
    "BELT-4":           "belt4",
    "Tread Code":       "tread_code",
}

TBR_SHEET4_COLS = {
    "S.No":       "s_no",
    "Category":   "category",
    "SKU List":   "sku",
    "Tyre of test": "test_type",
    "Frequency":  "frequency",
}


def _tbr_sheet(path: Path, sheet: str, col_map: dict[str, str]) -> pl.DataFrame:
    if not path.exists():
        log.warning("construction.tbr.missing", path=str(path), sheet=sheet)
        return pl.DataFrame()
    df = pl.read_excel(path, sheet_name=sheet)
    sorted_prefixes = sorted(col_map.items(), key=lambda kv: -len(kv[0]))
    ren: dict[str, str] = {}
    for c in df.columns:
        norm = " ".join(str(c).split())
        for prefix, tgt in sorted_prefixes:
            if norm.startswith(prefix) and tgt not in ren.values():
                ren[c] = tgt
                break
    df = df.rename(ren)
    keep = [c for c in col_map.values() if c in df.columns]
    df = df.select(keep)
    if "sku" in df.columns:
        df = df.filter(pl.col("sku").is_not_null())
    return df


def build_construction() -> tuple[Path, Path, Path, Path]:
    root = CONFIG.paths.raw_construction
    out_dir = CONFIG.paths.warehouse / "construction"
    out_dir.mkdir(parents=True, exist_ok=True)

    pcr = _pcr(root / PCR_XLSX)
    # NB: in the TBR xlsx the LOGICAL sheet names are shuffled — the SKU→GT map
    # lives on logical sheet "Sheet4", the GT template on "Sheet6", test SKUs
    # on "Sheet1". Physical order does not equal name order.
    tbr_sku = _tbr_sheet(root / TBR_XLSX, "Sheet4", TBR_SHEET5_COLS).with_columns(pl.lit("TBR").alias("plant"))
    tbr_gt  = _tbr_sheet(root / TBR_XLSX, "Sheet6", TBR_SHEET6_COLS)
    tbr_test = _tbr_sheet(root / TBR_XLSX, "Sheet1", TBR_SHEET4_COLS)

    pcr_out = out_dir / "construction_pcr.parquet"
    tbr_out = out_dir / "construction_tbr.parquet"
    gt_out  = out_dir / "construction_gt_template_tbr.parquet"
    tst_out = out_dir / "test_skus_tbr.parquet"

    if pcr.height:
        pcr.write_parquet(pcr_out, compression="zstd")
        log.info("construction.pcr.written", rows=pcr.height, path=str(pcr_out))
    if tbr_sku.height:
        tbr_sku.write_parquet(tbr_out, compression="zstd")
        log.info("construction.tbr.written", rows=tbr_sku.height, path=str(tbr_out))
    if tbr_gt.height:
        tbr_gt.write_parquet(gt_out, compression="zstd")
        log.info("construction.tbr_gt.written", rows=tbr_gt.height, path=str(gt_out))
    if tbr_test.height:
        tbr_test.write_parquet(tst_out, compression="zstd")
        log.info("construction.tbr_test.written", rows=tbr_test.height, path=str(tst_out))
    return pcr_out, tbr_out, gt_out, tst_out


def run() -> None:
    build_construction()
