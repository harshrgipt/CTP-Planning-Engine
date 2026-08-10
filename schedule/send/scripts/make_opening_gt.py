"""Derive per-month OPENING GREEN-TYRE INVENTORY from the MES history.

    python -m scripts.make_opening_gt [out_dir]

Writes into `masters/opening_gt/`, one pair per month:

    opening_gt_<YYYY-MM>.csv       plant, gt_code, sku, qty, age_p50_h,
                                   age_max_h, as_of
    opening_gt_<YYYY-MM>.parquet   ONE ROW PER TYRE: plant, gt_code, built_ts,
                                   as_of, age_h
    opening_gt_summary.csv         one row per (month, plant)

AS OF THE 1st AT 07:00, not midnight. The plant day runs 07:00 -> 07:00 (shift
A/B/C x 480 min), so "opening stock for July" is what is on the rack when the
July 1 A-shift starts. Cutting at midnight would count seven hours of the
previous day's C-shift output as opening stock and simultaneously miss the
tyres cured during it.

DECEMBER IS EXCLUDED: it is the first month of history, so there is no prior
month to carry stock in from. Any figure computed for it would be an artefact
of the data window, not a real opening balance.

DEFINITION: built before the cutoff, not yet cured at the cutoff. Uses the
documented per-tyre join `v_build.productionID = v_curing.gtbarCode`
(MEMORY s3) -- never `gt_code IS NOT NULL`, which is a cartesian product.

AGE BOUND. "Built and not cured" also matches tyres that were NEVER cured --
scrap and quality rejects -- which are not stock. We cannot see the future to
tell them apart, so we bound by age: a tyre older than the observed build->cure
p99 lag is not waiting, it is gone. Excluded counts are reported so the
judgement stays visible rather than silent. This mirrors
`GreenTireLedger.load_opening_from_mes`.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

DAY_START_H = 7          # plant day boundary


def _max_age_h(con) -> float:
    r = con.execute("""
        SELECT quantile_cont(
                   date_diff('second', b.event_ts, c.event_ts) / 3600.0, 0.99)
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.QualityStatus = '1'
          AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts
    """).fetchone()
    return float(r[0]) if r and r[0] else 168.0


def opening_at(con, as_of: datetime, max_age_h: float) -> tuple[pl.DataFrame, int]:
    """Per-tyre green stock on the rack at `as_of`. Returns (frame, n_excluded)."""
    raw = con.execute("""
        WITH built AS (
            SELECT plant, itemCode AS gt_code, productionID AS gtbar, event_ts
            FROM v_build
            WHERE stage = 2 AND QualityStatus = '1'
              AND event_ts < ?::TIMESTAMP AND itemCode IS NOT NULL
        ), cured AS (
            SELECT gtbarCode AS gtbar FROM v_curing
            WHERE event_ts < ?::TIMESTAMP AND statuscritical = 'Normal'
        )
        SELECT b.plant, b.gt_code, b.event_ts AS built_ts
        FROM built b LEFT JOIN cured c ON b.gtbar = c.gtbar
        WHERE c.gtbar IS NULL
    """, [as_of, as_of]).pl()
    if raw.height == 0:
        return raw, 0
    raw = raw.with_columns(
        ((pl.lit(as_of) - pl.col("built_ts")).dt.total_seconds() / 3600.0).alias("age_h"))
    keep = raw.filter(pl.col("age_h") <= max_age_h)
    return keep, raw.height - keep.height


def main(out_dir: Path) -> int:
    set_cutoff(None)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duck()
    max_age = _max_age_h(con)
    print(f"age bound = build->cure p99 lag = {max_age:.1f} h "
          f"(older than this is scrap, not stock)\n")

    months = [r[0] for r in con.execute("""
        SELECT DISTINCT date_trunc('month', event_ts)::DATE
        FROM v_build WHERE stage = 2 ORDER BY 1
    """).fetchall() if r[0] is not None]

    try:
        bom = con.execute("""
            SELECT gt_code, any_value(super_parent_sku) AS sku
            FROM v_bom_gt WHERE gt_code IS NOT NULL GROUP BY 1
        """).pl()
    except Exception:  # noqa: BLE001
        bom = pl.DataFrame({"gt_code": [], "sku": []})

    summary = []
    for mo in months[1:]:                      # skip the first month (December)
        as_of = datetime(mo.year, mo.month, 1, DAY_START_H, 0, 0)
        tag = f"{mo.year}-{mo.month:02d}"
        tyres, dropped = opening_at(con, as_of, max_age)
        if tyres.height == 0:
            print(f"  {tag}: no opening stock")
            continue

        per = (tyres.group_by(["plant", "gt_code"]).agg(
                    pl.len().alias("qty"),
                    pl.col("age_h").median().round(1).alias("age_p50_h"),
                    pl.col("age_h").max().round(1).alias("age_max_h"))
               .join(bom, on="gt_code", how="left")
               .with_columns(pl.col("sku").fill_null(""),
                             pl.lit(str(as_of)).alias("as_of"))
               .select(["plant", "gt_code", "sku", "qty", "age_p50_h",
                        "age_max_h", "as_of"])
               .sort(["plant", "qty"], descending=[False, True]))
        per.write_csv(out_dir / f"opening_gt_{tag}.csv")
        (tyres.with_columns(pl.lit(as_of).alias("as_of"))
              .write_parquet(out_dir / f"opening_gt_{tag}.parquet", compression="zstd"))

        for plant in sorted(tyres["plant"].unique().to_list()):
            s = tyres.filter(pl.col("plant") == plant)
            summary.append({
                "month": tag, "plant": plant, "as_of": str(as_of),
                "gts": s["gt_code"].n_unique(), "qty": s.height,
                "age_p50_h": round(float(s["age_h"].median()), 1),
                "age_p95_h": round(float(s["age_h"].quantile(0.95)), 1),
                "over_72h": int(s.filter(pl.col("age_h") > 72).height),
            })
        print(f"  opening_gt_{tag}.csv  {per.height:>4} GT rows  "
              f"{tyres.height:>7,} tyres  (excluded {dropped:,} over {max_age:.0f}h)")

    sm = pl.DataFrame(summary)
    sm.write_csv(out_dir / "opening_gt_summary.csv")
    print("\n" + str(sm))
    log.info("opening_gt.written", months=len(summary) // 2, dir=str(out_dir))
    return 0


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG.paths.masters / "opening_gt"
    sys.exit(main(d))
