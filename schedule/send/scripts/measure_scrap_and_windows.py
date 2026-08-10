"""STEP 1 + STEP 2 before touching the controller.

STEP 1  GT scrap / loss rate per class.
        build/cure ratio - 1 is NOT drift when inventory is trend-flat -- the
        excess must leave the system. Measure it directly: tyres built and
        NEVER cured in the whole history. Right-censoring matters: a tyre built
        in July may be cured in August, which we do not have, so only months
        with enough follow-up are trustworthy.

STEP 2  Is D_g clamped for small GTs?
        Under the rectangle model a GT curing 480/month needs 480/156 = 3.1
        press-days, so n_g = 1 and D_g ~ 3.1 days -- a short campaign, and
        cure-on-build with no carry follows automatically. If small GTs are
        instead spread over long windows, something is clamping D_g and the
        slow-mover "rule" is really a bug.
"""
from __future__ import annotations

import polars as pl

from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

RATE = {"PCR": 156.0, "TBR": 48.0}


def step1() -> None:
    con = duck()
    print("=" * 78)
    print("STEP 1 -- GT SCRAP / LOSS RATE  (built and NEVER cured)")
    print("=" * 78)
    df = con.execute("""
        WITH b AS (
            SELECT plant, date_trunc('month', event_ts)::DATE mo,
                   productionID pid
            FROM v_build WHERE stage=2 AND QualityStatus='1' AND productionID IS NOT NULL
        ), c AS (
            SELECT DISTINCT gtbarCode pid FROM v_curing WHERE statuscritical='Normal'
        )
        SELECT b.plant, b.mo, count(*) built,
               count(*) FILTER (WHERE c.pid IS NULL) never_cured
        FROM b LEFT JOIN c ON b.pid=c.pid
        GROUP BY 1,2 ORDER BY 1,2
    """).pl()
    df = df.with_columns(
        (100.0 * pl.col("never_cured") / pl.col("built")).round(3).alias("loss_pct"))
    months = sorted(df["mo"].unique().to_list())
    censored = months[-1]
    print(f"  (last month {censored} is RIGHT-CENSORED -- its tyres may cure next month)")
    for p in ["PCR", "TBR"]:
        s = df.filter(pl.col("plant") == p)
        print(f"\n  {p}:")
        for r in s.iter_rows(named=True):
            flag = "  <- censored, ignore" if r["mo"] == censored else ""
            print(f"    {str(r['mo'])[:7]}  built {r['built']:>7,}  "
                  f"never cured {r['never_cured']:>6,}  = {r['loss_pct']:>6.3f}%{flag}")
        ok = s.filter(pl.col("mo") != censored)
        w = float(ok["never_cured"].sum()) / float(ok["built"].sum()) * 100
        print(f"    ---> WEIGHTED LOSS RATE (excl. censored) = {w:.3f}%"
              f"   => build/cure target = {1 + w/100:.4f}")


def step2() -> None:
    print("\n" + "=" * 78)
    print("STEP 2 -- IS D_g CLAMPED FOR SMALL GTs?  (July plan, observed)")
    print("=" * 78)
    c = pl.read_parquet("runs/walkforward/month=2026-07/cure_schedule.parquet")
    per = (c.group_by(["plant", "gt_code"])
             .agg(pl.len().alias("cured"),
                  pl.col("start_ts").dt.date().n_unique().alias("cure_days"),
                  pl.col("press").n_unique().alias("presses")))
    for p in ["PCR", "TBR"]:
        s = per.filter(pl.col("plant") == p).with_columns(
            (pl.col("cured") / RATE[p]).alias("area_press_days"))
        s = s.with_columns(
            (pl.col("area_press_days") / pl.col("presses")).alias("D_expected"))
        print(f"\n  {p}:  area = cured/rate press-days;  "
              f"D_expected = area / n_presses")
        print(f"  {'area bucket':<16} {'GTs':>4} {'cured p50':>10} "
              f"{'n_press p50':>11} {'D_expected':>11} {'D_ACTUAL':>9}")
        for lo, hi, lbl in [(0, 5, "<5 pd (tiny)"), (5, 15, "5-15 pd"),
                            (15, 40, "15-40 pd"), (40, 1e9, ">40 pd (big)")]:
            b = s.filter((pl.col("area_press_days") >= lo)
                         & (pl.col("area_press_days") < hi))
            if b.height == 0:
                continue
            print(f"  {lbl:<16} {b.height:>4} {b['cured'].median():>10,.0f} "
                  f"{b['presses'].median():>11.0f} "
                  f"{b['D_expected'].median():>11.1f} {b['cure_days'].median():>9.0f}")
        tiny = s.filter(pl.col("area_press_days") < 5)
        if tiny.height:
            ratio = float(tiny["cure_days"].median()) / max(float(tiny["D_expected"].median()), 0.1)
            print(f"    tiny-GT stretch factor = {ratio:.1f}x  "
                  f"{'<-- CLAMPED' if ratio > 2 else '<-- ok, running free'}")


if __name__ == "__main__":
    set_cutoff(None)
    step1()
    step2()
    log.info("measure.done")
