"""Turn a plant ORDER-BOOK workbook into `masters/demand/demand_<month>.parquet`.

    python -m scripts.ingest_orderbook_demand \
        --xlsx "../../August_Demand_PCR_TBR_Classification.xlsx" \
        --sheet "August Demand Classified" --month 2026-08

HOW THIS DIFFERS FROM `scripts/make_demand.py`
  `make_demand.py` derives demand from what the plant actually CURED in MES. That
  is a backtest signal and it only exists for months MES covers. This script
  takes a forward ORDER BOOK -- what the plant has been asked to ship -- and is
  the correct source for planning a month that has not happened.

WHAT THE WORKBOOK GIVES AND WHAT IT DOES NOT
  Columns: SKUCode · SKU Description · Requirement · Order Type · Market ·
  Delivery date · Priority flag · Classification · Matched GT Code ·
  Matched Master Description / Size · Source Sheet.

  * `Delivery date` and `Priority flag` are EMPTY on every row of the August
    file. There is therefore NO due-date profile in this data. We do not invent
    one: each (plant, GT, SKU) gets ONE row carrying the whole month's
    requirement, dated the last day of the month. A flat daily spread would look
    like information and be none -- and `due_date` has no live reader, but L4.5 DOES read `day` (l45_lotsize.py, per-GT phase curve -> lot_deadlines), so an unphased book collapses every deadline to one value and L5's earliest-deadline sort degenerates -- checked 2026-08-18
    (L4 aggregates to (plant, gt_code); the columns exist for the exports).
  * `Requirement` is fractional on some rows (e.g. 433422.1 total). Kept as
    float through the mapping and rounded ONCE at the end with largest-remainder
    so the plant total is preserved exactly.

GT RESOLUTION -- MEASURED, NOT TRUSTED
  The workbook already carries `Matched GT Code`, and the brief says to use it.
  We do -- but as the SECOND source, because for TBR it is the BOM short code
  ("GT 5001") and the engine's TBR namespace is size-led ("10.00 R 20 JDE").
  See `scripts/gt_namespace.py` for the full trap.

  Order of resolution, per row:
    1. SKU -> engine gt_code through the curing-recipe chain (authoritative)
    2. `Matched GT Code` -> engine gt_code through the string bridge
  Both routes were run on the August file and compared on every row where BOTH
  resolve: **148 rows, 0 disagreements, 0 plant mismatches.** The workbook's own
  mapping is therefore corroborated, not merely accepted.

  A SKU that neither route resolves is written to
  `masters/demand/UNMAPPED_<month>.csv` with its quantity and is NOT planned. In
  almost every case it is a GT with no MES history, which also means no
  capability row, no cycle time and no mould -- genuinely unplannable rather
  than merely unmapped, and reporting it is the right answer.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import sys
from datetime import date
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_namespace import mes_namespace, resolve_gt_label, sku_to_gt  # noqa: E402


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def read_sheet(xlsx: Path, sheet: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
    wb.close()
    # the sheet carries a title row and a note row above the header
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0] == "SKUCode")
    hdr = rows[hdr_i]
    out = []
    for r in rows[hdr_i + 1:]:
        if not any(x for x in r):
            continue
        out.append(dict(zip(hdr, r)))
    return out


def largest_remainder(vals: list[float], total: int) -> list[int]:
    """Integerise so the sum is exactly `total` and nothing is silently lost."""
    base = [int(v) for v in vals]
    rem = total - sum(base)
    order = sorted(range(len(vals)), key=lambda i: -(vals[i] - base[i]))
    for i in range(rem):
        base[order[i % len(order)]] += 1
    return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--sheet", default="August Demand Classified")
    ap.add_argument("--month", required=True)
    ap.add_argument("--out", default=None, help="default masters/demand")
    a = ap.parse_args()

    xlsx = Path(a.xlsx)
    if not xlsx.is_absolute():
        xlsx = (Path.cwd() / xlsx).resolve()
    out_dir = Path(a.out) if a.out else ROOT / "masters" / "demand"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = read_sheet(xlsx, a.sheet)
    y, m = int(a.month[:4]), int(a.month[5:7])
    last = date(y, m, calendar.monthrange(y, m)[1])

    chain = sku_to_gt()
    ns = mes_namespace()

    recs, unmapped = [], []
    tiers: dict[str, int] = {}
    agree = disagree = 0
    plant_mismatch = []
    for r in rows:
        sku = r.get("SKUCode", "")
        cls = r.get("Classification", "")
        try:
            qty = float(r.get("Requirement", "") or 0)
        except ValueError:
            qty = 0.0
        mgt = r.get("Matched GT Code", "")
        if cls not in ("PCR", "TBR"):
            unmapped.append({"sku": sku, "qty": qty, "plant": cls,
                             "matched_gt": mgt, "reason": "not in either master",
                             "desc": r.get("SKU Description", "")})
            continue
        ch = chain.get(sku)
        by_label, tier = resolve_gt_label(mgt, cls, ns)
        if ch and by_label:
            if ch[1] == by_label:
                agree += 1
            else:
                disagree += 1
        if ch and ch[0] != cls:
            plant_mismatch.append((sku, cls, ch[0], ch[1]))
        gt = ch[1] if ch else by_label
        src = "recipe-chain" if ch else (f"label:{tier}" if by_label else "")
        if not gt:
            unmapped.append({"sku": sku, "qty": qty, "plant": cls,
                             "matched_gt": mgt, "reason": f"no MES history ({tier})",
                             "desc": r.get("SKU Description", "")})
            continue
        tiers[src] = tiers.get(src, 0) + 1
        recs.append({"plant": cls, "gt_code": gt, "sku": sku, "qty": qty,
                     "otype": r.get("Order Type", ""), "mkt": r.get("Market", "")})

    df = pl.DataFrame(recs) if recs else pl.DataFrame(
        schema={"plant": pl.Utf8, "gt_code": pl.Utf8, "sku": pl.Utf8,
                "qty": pl.Float64, "otype": pl.Utf8, "mkt": pl.Utf8})
    g = (df.group_by(["plant", "gt_code", "sku"])
         .agg(pl.col("qty").sum().alias("q"))
         .filter(pl.col("q") > 0)
         .sort(["plant", "gt_code", "sku"]))

    # integerise ONCE, per plant, so each plant's total is preserved exactly
    parts = []
    for p in ("PCR", "TBR"):
        s = g.filter(pl.col("plant") == p)
        if not s.height:
            continue
        tot = int(round(float(s["q"].sum())))
        s = s.with_columns(pl.Series("qty", largest_remainder(s["q"].to_list(), tot),
                                     dtype=pl.Int64))
        parts.append(s)
    fin = (pl.concat(parts) if parts else g.with_columns(pl.col("q").cast(pl.Int64).alias("qty")))
    fin = fin.select([
        "plant", "gt_code", "sku", "qty",
        pl.lit(a.month).alias("month"),
        pl.lit(last).alias("due_date"),
        pl.lit(last.day).cast(pl.Int64).alias("day"),
    ]).sort(["plant", "gt_code", "sku"])

    fin.write_parquet(out_dir / f"demand_{a.month}.parquet", compression="zstd")
    fin.write_csv(out_dir / f"demand_{a.month}.csv")
    if unmapped:
        with open(out_dir / f"UNMAPPED_{a.month}.csv", "w", newline="",
                  encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(unmapped[0].keys()))
            w.writeheader()
            w.writerows(unmapped)

    print("=" * 92)
    print(f"ORDER-BOOK DEMAND  --  {a.month}   {xlsx.name} :: {a.sheet}")
    print("=" * 92)
    print(f"  workbook rows read          {len(rows)}")
    print(f"  classified PCR/TBR          {len(rows) - sum(1 for u in unmapped if u['reason'].startswith('not in'))}")
    print(f"  GT resolution tiers         " + "  ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    print(f"  cross-check both routes     agree {agree}  DISAGREE {disagree}  "
          f"plant mismatch {len(plant_mismatch)}")
    for x in plant_mismatch:
        print(f"      !! {x}")
    print()
    print(f"  {'plant':<6}{'GTs':>6}{'SKUs':>7}{'rows':>7}{'tyres':>12}")
    for p in ("PCR", "TBR"):
        s = fin.filter(pl.col("plant") == p)
        print(f"  {p:<6}{s['gt_code'].n_unique():>6}{s['sku'].n_unique():>7}"
              f"{s.height:>7}{int(s['qty'].sum()):>12,}")
    print(f"  {'TOTAL':<6}{fin['gt_code'].n_unique():>6}{fin['sku'].n_unique():>7}"
          f"{fin.height:>7}{int(fin['qty'].sum()):>12,}")
    if unmapped:
        uq = sum(u["qty"] for u in unmapped)
        print(f"\n  UNMAPPED {len(unmapped)} rows, {uq:,.0f} tyres "
              f"-> UNMAPPED_{a.month}.csv  (NOT planned)")
        for u in sorted(unmapped, key=lambda z: -z["qty"])[:20]:
            if u["qty"] > 0:
                print(f"      {u['plant']:<14}{u['sku']:<20}{u['qty']:>10,.0f}"
                      f"  {u['matched_gt']:<18}{u['reason']}")
    print(f"\n  -> {out_dir / f'demand_{a.month}.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
