"""Manual plant GT stock-count workbooks -> an opening-GT master the engine reads.

    python -m scripts.ingest_manual_opening_gt --month 2026-08 --age-h 24 \
        --pcr "../../gtinvaug/gt_inventory_manual_pcr_20260801.xlsx" \
        --tbr "../../gtinvaug/gt_inventory_manual_tbr_20260801.xlsx"

Writes `masters/opening_gt/opening_gt_manual_<month>.parquet`, ONE ROW PER TYRE
(plant · gt_code · built_ts · age_h · as_of) -- the same schema as the
MES-derived `opening_gt_<month>.parquet`, so L4/L5/L7 read it unchanged via

    PLANNER_OPENING_GT=opening_gt_manual_<month>.parquet

It is written under its OWN name and NEVER over `opening_gt_<month>.parquet`.
The MES-derived master stays on disk so the two can be compared; this script
prints that comparison. THE MANUAL COUNT IS THE SOLE SOURCE for a run that uses
it -- there is no blending, no top-up and no fall-back. A GT absent from the
workbook has ZERO opening stock, which is what a physical count means.

=============================================================================
THE AGE ASSUMPTION -- the one thing the workbook does not give us
=============================================================================
The workbooks carry `ItemCode` and `TotalQuantity` and nothing else. R5 (72 h
green-tyre shelf life) needs an age per tyre, so one must be ASSUMED and the
assumption must be visible in the output rather than buried.

`--age-h` writes that single age onto every tyre. Default 24 h, i.e. 48 h of the
72 h window still available at t0. Why 24:

  * the count is ~6.1 k tyres against ~533 k of monthly demand, i.e. roughly
    8.5 h of combined build output -- one shift of in-transit WIP, which is what
    a floor count at 07:00 physically is;
  * the MES-derived snapshot of the SAME instant measures median age 14.1 h
    (PCR) / 13.4 h (TBR), p95 ~39 h, max ~56 h. 24 h sits above both medians,
    so the assumption is pessimistic against the only measurement available.
    (This uses MES for the AGE DISTRIBUTION only. Not one tyre of MES QUANTITY
    enters the file.)

Because it is an assumption, CHECK WHETHER IT BINDS rather than defending it:
re-run with `--age-h 48` and compare fulfilment. If the plan is unchanged the
assumption is not a constraint and the choice does not matter (PARTITION
DO-NOT #30 -- verify a gate is binding before arguing about its exact value).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gt_namespace import mes_namespace, resolve_gt_label  # noqa: E402


def read_count(xlsx: Path) -> list[tuple[str, int]]:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [r for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [("" if c is None else str(c).strip()) for c in rows[0]]
    i_code = hdr.index("ItemCode")
    i_qty = hdr.index("TotalQuantity")
    out = []
    for r in rows[1:]:
        code = "" if r[i_code] is None else str(r[i_code]).strip()
        if not code:
            continue
        q = r[i_qty]
        if q is None:
            continue
        out.append((code, int(q)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--pcr", required=True)
    ap.add_argument("--tbr", required=True)
    ap.add_argument("--age-h", type=float, default=24.0)
    ap.add_argument("--tag", default="manual")
    a = ap.parse_args()

    y, m = int(a.month[:4]), int(a.month[5:7])
    as_of = datetime(y, m, 1, 7, 0)          # plant day starts 07:00
    built = as_of - timedelta(hours=a.age_h)
    ns = mes_namespace()

    rows, misses, tiers = [], [], {}
    totals, resolved_tot = {}, {}
    for plant, f in (("PCR", a.pcr), ("TBR", a.tbr)):
        p = Path(f)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        cnt = read_count(p)
        totals[plant] = sum(q for _, q in cnt)
        rt = 0
        for code, q in cnt:
            gt, tier = resolve_gt_label(code, plant, ns)
            if not gt:
                misses.append({"plant": plant, "item_code": code, "qty": q,
                               "reason": tier})
                continue
            tiers[tier] = tiers.get(tier, 0) + 1
            rt += q
            rows.extend([{"plant": plant, "gt_code": gt, "built_ts": built,
                          "age_h": a.age_h, "as_of": as_of}] * q)
        resolved_tot[plant] = rt

    df = pl.DataFrame(rows, schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                                    "built_ts": pl.Datetime("us"),
                                    "age_h": pl.Float64,
                                    "as_of": pl.Datetime("us")})
    out = ROOT / "masters" / "opening_gt" / f"opening_gt_{a.tag}_{a.month}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out, compression="zstd")

    print("=" * 92)
    print(f"MANUAL OPENING GT  --  {a.month}   as_of {as_of:%Y-%m-%d %H:%M}   "
          f"assumed age {a.age_h:.0f} h  ({72 - a.age_h:.0f} h of R5 left)")
    print("=" * 92)
    print(f"  resolution tiers: " + "  ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    print(f"\n  {'plant':<6}{'workbook rows':>15}{'counted':>10}{'resolved':>10}"
          f"{'cover %':>9}{'GTs':>6}")
    for p in ("PCR", "TBR"):
        s = df.filter(pl.col("plant") == p)
        n_rows = sum(1 for x in misses if x["plant"] == p) + s["gt_code"].n_unique()
        print(f"  {p:<6}{n_rows:>15}{totals.get(p, 0):>10,}{resolved_tot.get(p, 0):>10,}"
              f"{100 * resolved_tot.get(p, 0) / max(totals.get(p, 1), 1):>8.1f}%"
              f"{s['gt_code'].n_unique():>6}")
    print(f"  {'TOTAL':<6}{'':>15}{sum(totals.values()):>10,}"
          f"{sum(resolved_tot.values()):>10,}"
          f"{100 * sum(resolved_tot.values()) / max(sum(totals.values()), 1):>8.1f}%"
          f"{df['gt_code'].n_unique():>6}")
    if misses:
        print(f"\n  UNRESOLVED (dropped, NOT planned):")
        for x in misses:
            print(f"      {x['plant']}  {x['item_code']:<32}{x['qty']:>8,}  {x['reason']}")

    # ---- side-by-side with the MES-derived master (reporting only) ---------
    mes = ROOT / "masters" / "opening_gt" / f"opening_gt_{a.month}.parquet"
    if mes.exists():
        mm = pl.read_parquet(mes)
        print(f"\n  MANUAL vs MES-DERIVED master ({mes.name}) -- REPORTING ONLY, "
              f"the plan uses the manual file alone")
        print(f"  {'plant':<6}{'manual':>10}{'MES':>10}{'delta':>10}{'delta %':>10}"
              f"{'manual GTs':>12}{'MES GTs':>9}")
        for p in ("PCR", "TBR"):
            a_ = int(df.filter(pl.col("plant") == p).height)
            b_ = int(mm.filter(pl.col("plant") == p).height)
            print(f"  {p:<6}{a_:>10,}{b_:>10,}{a_ - b_:>10,}"
                  f"{100 * (a_ - b_) / max(b_, 1):>9.1f}%"
                  f"{df.filter(pl.col('plant') == p)['gt_code'].n_unique():>12}"
                  f"{mm.filter(pl.col('plant') == p)['gt_code'].n_unique():>9}")
        print(f"  {'TOTAL':<6}{df.height:>10,}{mm.height:>10,}"
              f"{df.height - mm.height:>10,}"
              f"{100 * (df.height - mm.height) / max(mm.height, 1):>9.1f}%")
        print(f"\n  MES age distribution at the same instant (context for --age-h):")
        for p in ("PCR", "TBR"):
            s = mm.filter(pl.col("plant") == p)["age_h"]
            if s.len():
                print(f"      {p}: p50 {s.median():.1f} h  p95 "
                      f"{s.quantile(0.95):.1f} h  max {s.max():.1f} h")
    print(f"\n  -> {out}")
    print(f"     use with  PLANNER_OPENING_GT={out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
