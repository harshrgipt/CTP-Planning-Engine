"""TBR uniformity/balance data ingest (sheets 2, 7, 8 of the TBR construction xlsx)."""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.runs.logger import log


TBR_XLSX = "SKU wise construction mapping TBR.xlsx"

BALANCE_COLS = {
    "WCNAME":       "wc_name",
    "DATE":         "date",
    "TIME":         "time",
    "RECIPENO":     "recipe_no",
    "RECIPECODE":   "recipe_code",
    "BARCODE":      "barcode",
    "TOTALRANK":    "total_rank",
    "UPPERAMOUNT":  "upper_amount",
    "UPPERANGLE":   "upper_angle",
    "UPPERRANK":    "upper_rank",
    "LOWERAMOUNT":  "lower_amount",
    "LOWERANGLE":   "lower_angle",
    "LOWERRANK":    "lower_rank",
    "UPLOAMOUNT":   "uplo_amount",
    "UPLORANK":     "uplo_rank",
    "STATICAMOUNT": "static_amount",
    "STATICANGLE":  "static_angle",
    "STATICRANK":   "static_rank",
    "COUPLEAMOUNT": "couple_amount",
    "COUPLEANGLE":  "couple_angle",
}


def _read_sheet(path: Path, sheet: str) -> pl.DataFrame:
    df = pl.read_excel(path, sheet_name=sheet)
    ren = {}
    for c in df.columns:
        norm = " ".join(str(c).split()).upper()
        for prefix, tgt in BALANCE_COLS.items():
            if norm.startswith(prefix):
                ren[c] = tgt
                break
    df = df.rename(ren)
    keep = [c for c in BALANCE_COLS.values() if c in df.columns]
    df = df.select(keep).with_columns(pl.lit(sheet).alias("source_sheet"))
    return df


def build_balance() -> Path:
    src = CONFIG.paths.raw_construction / TBR_XLSX
    if not src.exists():
        log.warning("balance.missing", path=str(src))
        return Path()
    frames = []
    # Logical sheet names holding raw balance data (see TBR xlsx workbook.xml):
    for sh in ("Before", "After", "Sheet3"):
        try:
            f = _read_sheet(src, sh)
            if f.height:
                frames.append(f)
        except Exception as e:  # noqa: BLE001
            log.warning("balance.sheet_error", sheet=sh, err=str(e))
    if not frames:
        return Path()
    df = pl.concat(frames, how="diagonal_relaxed")
    out_dir = CONFIG.paths.warehouse / "balance" / "plant=TBR"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "balance_tbr.parquet"
    df.write_parquet(out, compression="zstd")
    log.info("balance.written", rows=df.height, path=str(out))
    return out


def run() -> None:
    build_balance()
