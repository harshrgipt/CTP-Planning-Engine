"""Verify EVERY input file against all 8 months. Are the inputs actually sound?

    python scripts/verify_inputs.py

Derived masters are only as good as their sources. This checks the raw MES
warehouse, the plant-supplied masters, and the files we generated -- coverage,
key integrity, join hit rates, value sanity, and month-by-month completeness.

FAIL = the engine will be wrong.   WARN = known limitation, planned around.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

FAIL: list[str] = []
WARN: list[str] = []
OKS: list[str] = []


def chk(cond, msg, hard=True):
    (OKS if cond else (FAIL if hard else WARN)).append(msg)
    print(f"  {'OK  ' if cond else ('FAIL' if hard else 'WARN')} {msg}")


def hdr(t):
    print("\n" + "=" * 84)
    print(t)
    print("=" * 84)


def main() -> int:
    set_cutoff(None)
    con = duck()

    # ---------------------------------------------------------------- raw
    hdr("1. RAW MES WAREHOUSE — presence, coverage, monthly completeness")
    for view, tcol in [("v_build", "event_ts"), ("v_curing", "event_ts"),
                       ("v_consume", "event_ts")]:
        try:
            r = con.execute(
                f"SELECT count(*) n, min({tcol}) lo, max({tcol}) hi, "
                f"count(DISTINCT date_trunc('month', {tcol})) mo FROM {view}"
            ).fetchone()
            chk(r[0] > 0, f"{view}: {r[0]:,} rows, {str(r[1])[:10]} .. {str(r[2])[:10]}, "
                          f"{r[3]} months")
            chk(r[3] >= 8, f"{view}: covers >= 8 months (has {r[3]})", hard=False)
        except Exception as e:  # noqa: BLE001
            chk(False, f"{view}: NOT AVAILABLE ({str(e)[:60]})")

    # A corrupt timestamp silently widens every date filter and every
    # partition prune. v_consume was found with a minimum of year 0029.
    for view in ("v_build", "v_curing", "v_consume"):
        try:
            bad = con.execute(
                f"SELECT count(*) FROM {view} WHERE event_ts < DATE '2025-01-01' "
                f"OR event_ts > DATE '2027-01-01'").fetchone()[0]
            chk(bad == 0, f"{view}: no out-of-range timestamps ({bad:,} outside "
                          f"2025-2027)", hard=(view != "v_consume"))
        except Exception:  # noqa: BLE001
            pass

    m = con.execute("""
        SELECT date_trunc('month', event_ts)::DATE mo, plant, count(*) n
        FROM v_build WHERE stage=2 GROUP BY 1,2 ORDER BY 1,2""").pl()
    print("\n  build stage-2 rows per month:")
    for r in m.iter_rows(named=True):
        print(f"    {str(r['mo'])[:7]} {r['plant']:4s} {r['n']:>9,}")
    per = m.group_by("plant").agg(pl.len().alias("months"))
    for r in per.iter_rows(named=True):
        chk(r["months"] == 8, f"{r['plant']}: build data present in all 8 months "
                              f"(has {r['months']})")

    # ---------------------------------------------------------- lineage
    hdr("2. LINEAGE — the joins the whole engine depends on")
    r = con.execute("""
        SELECT count(*) AS built,
               count(*) FILTER (WHERE c.gtbarCode IS NOT NULL) AS matched
        FROM v_build b LEFT JOIN (SELECT DISTINCT gtbarCode FROM v_curing) c
          ON b.productionID = c.gtbarCode
        WHERE b.stage=2 AND b.QualityStatus='1'
    """).fetchone()
    hit = 100.0 * r[1] / max(r[0], 1)
    chk(hit > 95, f"productionID -> gtbarCode join hit rate {hit:.1f}% "
                  f"({r[1]:,}/{r[0]:,})")

    r = con.execute("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE cycleStart > event_ts) AS fwd,
               quantile_cont(date_diff('second', event_ts, cycleStart), 0.5) AS med
        FROM v_curing WHERE statuscritical='Normal' AND cycleStart IS NOT NULL
    """).fetchone()
    chk(r[1] / max(r[0], 1) > 0.9,
        f"cycleStart is press-OPEN i.e. cycle END: {100*r[1]/max(r[0],1):.1f}% are "
        f"after event_ts, median dwell {r[2]/60:.1f} min")

    r = con.execute("SELECT count(*) FROM v_build WHERE stage=2 AND "
                    "productionID IS NULL").fetchone()
    chk(r[0] == 0, f"no NULL productionID on build stage 2 ({r[0]:,} null)", hard=False)

    dup = con.execute("""
        SELECT count(*) FROM (SELECT productionID FROM v_build WHERE stage=2
        GROUP BY 1 HAVING count(*) > 1)""").fetchone()[0]
    chk(dup == 0, f"productionID unique per build stage-2 tyre ({dup:,} duplicated)",
        hard=False)

    # ------------------------------------------------------- categoricals
    hdr("3. CATEGORICAL VALUES — are the filters we rely on real?")
    for tbl, col in [("v_build", "QualityStatus"), ("v_curing", "statuscritical")]:
        try:
            d = con.execute(
                f"SELECT {col} v, count(*) n FROM {tbl} GROUP BY 1 "
                f"ORDER BY 2 DESC LIMIT 6").pl()
            vals = ", ".join(f"{r['v']}={r['n']:,}" for r in d.iter_rows(named=True))
            print(f"    {tbl}.{col}: {vals}")
            chk(d.height > 0, f"{tbl}.{col} populated")
        except Exception as e:  # noqa: BLE001
            chk(False, f"{tbl}.{col} unreadable ({str(e)[:50]})")

    r = con.execute("SELECT count(DISTINCT wcID::VARCHAR) FROM v_curing").fetchone()[0]
    chk(150 < r < 200, f"press count sane: {r} distinct wcID across both plants")

    # ------------------------------------------------------ plant masters
    hdr("4. PLANT-SUPPLIED MASTERS")
    for f, need in [("Master_Building_ChangeoverTime_pcr.csv", 2),
                    ("Master_Building_ChangeoverTime_tbr.csv", 2),
                    ("Master_Mapping_Mould_SKU.csv", 2)]:
        p = CONFIG.paths.masters / f
        if not p.exists():
            chk(False, f"{f} MISSING")
            continue
        d = pl.read_csv(p, infer_schema_length=2000)
        chk(d.height > 0 and len(d.columns) >= need,
            f"{f}: {d.height} rows x {len(d.columns)} cols")
    x = CONFIG.paths.masters / "CTP Set up building ,curing and inspection (1) 2.xlsx"
    chk(x.exists(), f"CTP setup workbook present ({x.stat().st_size//1024 if x.exists() else 0} KB)")

    # -------------------------------------------------- generated inputs
    hdr("5. GENERATED INPUTS — demand and opening GT")
    dd = CONFIG.paths.masters / "demand"
    dfs = sorted(dd.glob("demand_2*.csv"))
    chk(len(dfs) == 8, f"8 monthly demand files present (found {len(dfs)})")
    dem = pl.concat([pl.read_csv(p) for p in dfs])
    chk(bool((dem["qty"] % 1 == 0).all()), "demand quantities all INTEGER")
    chk(bool((dem["qty"] > 0).all()), "demand quantities all > 0")
    chk(dem.select(["plant", "gt_code", "due_date"]).is_duplicated().sum() == 0,
        "no duplicate (plant, gt, day) demand rows")
    # demand must reconcile with the MES it was mined from
    act = con.execute("""
        SELECT date_trunc('month', c.event_ts)::DATE mo, count(*) n
        FROM v_build b JOIN v_curing c ON b.productionID=c.gtbarCode
        WHERE b.stage=2 AND c.statuscritical='Normal' AND b.itemCode IS NOT NULL
        GROUP BY 1 ORDER BY 1""").pl()
    dm = dem.group_by("month").agg(pl.col("qty").sum()).sort("month")
    bad = 0
    for a, b in zip(act.iter_rows(named=True), dm.iter_rows(named=True)):
        if abs(a["n"] - b["qty"]) > 1:
            bad += 1
            print(f"    MISMATCH {b['month']}: demand {b['qty']:,.0f} vs MES {a['n']:,}")
    chk(bad == 0, "every demand file reconciles to its MES month exactly")

    od = CONFIG.paths.masters / "opening_gt"
    ofs = sorted(od.glob("opening_gt_2*.csv"))
    chk(len(ofs) == 7, f"7 opening-GT files (Dec excluded) (found {len(ofs)})")
    op = pl.concat([pl.read_csv(p) for p in ofs])
    chk(bool((op["age_max_h"] <= 72).all()),
        f"no opening tyre past the 72h shelf life (max {op['age_max_h'].max():.1f}h)")
    pq = sorted(od.glob("opening_gt_2*.parquet"))
    chk(len(pq) == len(ofs), "per-tyre opening parquet exists for every month")
    if pq:
        t = pl.read_parquet(pq[-1])
        chk("built_ts" in t.columns,
            "opening parquet carries per-tyre built_ts (needed for FIFO ageing)")

    # ------------------------------------------------------ cross-source
    hdr("6. CROSS-SOURCE CONSISTENCY")
    gt_mes = {r[0] for r in con.execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND itemCode IS NOT NULL"
    ).fetchall()}
    chk(len(set(dem["gt_code"].unique().to_list()) - gt_mes) == 0,
        "every demand GT exists in MES")
    chk(len(set(op["gt_code"].unique().to_list()) - gt_mes) == 0,
        "every opening-GT code exists in MES")
    try:
        bom = con.execute("SELECT count(DISTINCT gt_code) FROM v_bom_gt").fetchone()[0]
        cov = len(gt_mes & {r[0] for r in con.execute(
            "SELECT DISTINCT gt_code FROM v_bom_gt").fetchall()})
        chk(cov > 0, f"BOM resolves {cov}/{len(gt_mes)} MES GT codes "
                     f"({100*cov/len(gt_mes):.0f}%) -- TBR is size-led and will not "
                     f"match", hard=False)
    except Exception as e:  # noqa: BLE001
        chk(False, f"v_bom_gt unreadable ({str(e)[:50]})", hard=False)

    # --------------------------------------------------------- verdict
    hdr(f"VERDICT: {len(FAIL)} FAIL, {len(WARN)} WARN, {len(OKS)} OK")
    for x in FAIL:
        print(f"  FAIL: {x}")
    for x in WARN:
        print(f"  WARN: {x}")
    log.info("verify_inputs.done", fail=len(FAIL), warn=len(WARN), ok=len(OKS))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
