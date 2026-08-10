"""PHASE 0 -- reverse-engineer the plant's planning strategy from MES history.

    python scripts/phase0_diagnosis.py [--months 8]

Answers, from data only, the question that decides the engine's architecture:
WHAT OPERATIONAL BEHAVIOUR DOES THE DATA DEMONSTRATE?  Building-driven,
curing-driven, hybrid, campaign-driven, or bottleneck-driven.

Ten analyses, each independently falsifiable, then a verdict that must be
supported by a majority of them.  Where two analyses disagree the disagreement
is REPORTED, not averaged away -- a split verdict is a finding about the plant,
not a defect in the method.

MEASUREMENT NOTES (these are the difference between a real answer and a
plausible one):
  * lead time is per TYRE via productionID = gtbarCode (99.6% hit rate), not
    FIFO-paired.  Every tyre's own wait is observed.
  * curing event_ts is press-CLOSE (cure starts); cycleStart is press-OPEN
    (cure ends).  The source names are backwards; GT waiting ends at event_ts.
  * every filter is on the Hive partition column `date`, never event_ts, so
    partition pruning applies.
  * campaigns are maximal same-GT runs on one machine/press ordered by time.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

from planner.data.warehouse import duck, set_cutoff

ROOT = Path(__file__).resolve().parent.parent
PLANTS = ["PCR", "TBR"]
SEP = "=" * 92


def hdr(n: int, title: str) -> None:
    print("\n" + SEP)
    print(f"{n}. {title}")
    print(SEP)


# ---------------------------------------------------------------------------
def a1_lead_lag() -> dict:
    """Per-tyre GT waiting time, via the barcode join. The core measurement."""
    hdr(1, "LEAD-LAG -- per-tyre GT waiting time (build -> press close)")
    q = duck().execute("""
        SELECT b.plant,
               date_trunc('month', b.event_ts)               AS mon,
               count(*)                                      AS n,
               median(date_diff('second', b.event_ts, c.event_ts))/3600.0 AS p50,
               quantile_cont(date_diff('second', b.event_ts, c.event_ts), 0.05)/3600.0 AS p05,
               quantile_cont(date_diff('second', b.event_ts, c.event_ts), 0.95)/3600.0 AS p95,
               avg(CASE WHEN c.event_ts < b.event_ts THEN 1.0 ELSE 0.0 END)  AS neg_frac
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.productionID IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1, 2""").pl()
    print(f"  {'plant':<6}{'month':<9}{'tyres':>10}{'p05 h':>8}{'p50 h':>8}"
          f"{'p95 h':>9}{'built AFTER cure':>18}")
    out = {}
    for r in q.iter_rows(named=True):
        print(f"  {r['plant']:<6}{str(r['mon'])[:7]:<9}{r['n']:>10,}{r['p05']:>8.1f}"
              f"{r['p50']:>8.1f}{r['p95']:>9.1f}{100*r['neg_frac']:>17.2f}%")
    for p in PLANTS:
        s = q.filter(pl.col("plant") == p)
        if s.height:
            out[p] = {"p50": float(s["p50"].median()),
                      "p95": float(s["p95"].median()),
                      "cv": float(s["p50"].std() / max(s["p50"].mean(), 1e-9))}
            print(f"  -> {p}: median lead {out[p]['p50']:.1f} h "
                  f"({out[p]['p50']/24:.2f} days)   p95 {out[p]['p95']:.1f} h   "
                  f"month-to-month CV {out[p]['cv']:.2f}")
    print("\n  INTERPRETATION: >48 h => building-driven | <8 h => curing-driven/JIT")
    for p, v in out.items():
        verdict = ("BUILDING-DRIVEN" if v["p50"] > 48 else
                   "CURING-DRIVEN / JIT" if v["p50"] < 8 else "HYBRID")
        print(f"    {p}: {verdict}")
        out[p]["verdict"] = verdict
    return out


# ---------------------------------------------------------------------------
def _campaigns(plant: str, side: str) -> pl.DataFrame:
    """Maximal same-GT runs. side='build' or 'cure'."""
    if side == "build":
        sql = f"""
        WITH s AS (SELECT machineCode AS res, itemCode AS gt, event_ts, date,
          lag(itemCode) OVER (PARTITION BY machineCode ORDER BY event_ts) pg
          FROM v_build WHERE stage=2 AND plant='{plant}'
          AND itemCode IS NOT NULL AND machineCode IS NOT NULL),
        r AS (SELECT *, sum(CASE WHEN pg IS DISTINCT FROM gt THEN 1 ELSE 0 END)
                 OVER (PARTITION BY res ORDER BY event_ts) AS run FROM s)
        SELECT res, gt, run, count(*) AS qty, min(event_ts) AS t0,
               max(event_ts) AS t1, min(date) AS d0
        FROM r GROUP BY res, gt, run"""
    else:
        # v_curing carries no itemCode -- the GT is reachable ONLY through
        # gtbarCode = productionID.  cure_gt is materialised once in main().
        sql = f"""
        WITH s AS (SELECT res, gt, event_ts, date,
          lag(gt) OVER (PARTITION BY res ORDER BY event_ts) pg
          FROM cure_gt WHERE plant='{plant}'),
        r AS (SELECT *, sum(CASE WHEN pg IS DISTINCT FROM gt THEN 1 ELSE 0 END)
                 OVER (PARTITION BY res ORDER BY event_ts) AS run FROM s)
        SELECT res, gt, run, count(*) AS qty, min(event_ts) AS t0,
               max(event_ts) AS t1, min(date) AS d0
        FROM r GROUP BY res, gt, run"""
    return duck().execute(sql).pl()


def a2_campaign_correlation(camps: dict) -> dict:
    """Do build and cure visit the same GTs in the same order?"""
    hdr(2, "CAMPAIGN CORRELATION -- does curing mirror building's sequence?")
    out = {}
    for p in PLANTS:
        b, c = camps[(p, "build")], camps[(p, "cure")]
        if not b.height or not c.height:
            continue
        # daily GT mix: how similar are the two sides' daily compositions?
        bd = (b.group_by(["d0", "gt"]).agg(pl.col("qty").sum())
              .rename({"qty": "bq"}))
        cd = (c.group_by(["d0", "gt"]).agg(pl.col("qty").sum())
              .rename({"qty": "cq"}))
        j = bd.join(cd, on=["d0", "gt"], how="outer_coalesce").fill_null(0)
        # cosine similarity of the daily GT-mix vectors
        sims = []
        for (_d,), g in j.group_by("d0"):
            x = np.array(g["bq"], float); y = np.array(g["cq"], float)
            if x.sum() == 0 or y.sum() == 0:
                continue
            sims.append(float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y))))
        cos = float(np.median(sims)) if sims else 0.0
        # order agreement: per GT, rank of first build day vs first cure day
        fb = b.group_by("gt").agg(pl.col("t0").min().alias("b0"))
        fc = c.group_by("gt").agg(pl.col("t0").min().alias("c0"))
        m = fb.join(fc, on="gt", how="inner")
        if m.height > 3:
            rb = np.argsort(np.argsort(np.array(
                m["b0"].dt.timestamp("ms"), float)))
            rc = np.argsort(np.argsort(np.array(
                m["c0"].dt.timestamp("ms"), float)))
            rho = float(np.corrcoef(rb, rc)[0, 1])
        else:
            rho = float("nan")
        out[p] = {"cos": cos, "rho": rho}
        print(f"  {p}:  daily GT-mix cosine similarity  {cos:.3f}   "
              f"first-appearance rank correlation  {rho:+.3f}   "
              f"({m.height} GTs)")
    print("\n  INTERPRETATION: cosine >0.8 => the two sides run the same daily mix")
    print("                  rho    >0.7 => curing follows building's GT order")
    return out


# ---------------------------------------------------------------------------
def a3_stickiness(camps: dict) -> dict:
    """Does a machine own a GT, or is everything routed everywhere?"""
    hdr(3, "MACHINE STICKINESS -- does one resource own a GT?")
    out = {}
    print(f"  {'plant':<6}{'side':<7}{'GTs':>6}{'resources':>11}"
          f"{'top-1 share':>13}{'HHI':>7}{'res/GT p50':>12}")
    for p in PLANTS:
        for side in ["build", "cure"]:
            d = camps[(p, side)]
            if not d.height:
                continue
            g = d.group_by(["gt", "res"]).agg(pl.col("qty").sum().alias("q"))
            tot = g.group_by("gt").agg(pl.col("q").sum().alias("t"),
                                       pl.len().alias("nres"))
            j = g.join(tot, on="gt").with_columns(
                (pl.col("q") / pl.col("t")).alias("sh"))
            top1 = float(j.group_by("gt").agg(pl.col("sh").max())["sh"].median())
            hhi = float(j.group_by("gt").agg(
                (pl.col("sh") ** 2).sum().alias("h"))["h"].median())
            out[(p, side)] = {"top1": top1, "hhi": hhi}
            print(f"  {p:<6}{side:<7}{tot.height:>6}{d['res'].n_unique():>11}"
                  f"{100*top1:>12.0f}%{hhi:>7.2f}"
                  f"{float(tot['nres'].median()):>12.0f}")
    print("\n  INTERPRETATION: top-1 share >0.8 / HHI >0.7 => dedicated routing")
    print("                  top-1 share <0.4 / HHI <0.3 => free routing")
    return out


# ---------------------------------------------------------------------------
def a4_changeovers(camps: dict) -> dict:
    """Who changes over more often -- the master should change LESS."""
    hdr(4, "CHANGEOVER PATTERN -- campaigns per resource-day, each side")
    out = {}
    print(f"  {'plant':<6}{'side':<7}{'campaigns':>11}{'res-days':>10}"
          f"{'per res-day':>13}{'mean campaign qty':>19}")
    for p in PLANTS:
        for side in ["build", "cure"]:
            d = camps[(p, side)]
            if not d.height:
                continue
            rd = d.select(["res", "d0"]).unique().height
            per = d.height / max(rd, 1)
            out[(p, side)] = {"per_day": per, "mean_qty": float(d["qty"].mean())}
            print(f"  {p:<6}{side:<7}{d.height:>11,}{rd:>10,}{per:>13.2f}"
                  f"{float(d['qty'].mean()):>19,.0f}")
    for p in PLANTS:
        if (p, "build") in out and (p, "cure") in out:
            b, c = out[(p, "build")]["per_day"], out[(p, "cure")]["per_day"]
            who = ("BUILDING is steadier -> building is master" if b < c * 0.8
                   else "CURING is steadier -> curing is master" if c < b * 0.8
                   else "NEITHER dominates -> synchronized / hybrid")
            print(f"  -> {p}: build {b:.2f} vs cure {c:.2f} per resource-day  |  {who}")
    return out


# ---------------------------------------------------------------------------
def a5_cross_correlation() -> dict:
    """Does today's build explain tomorrow's cure, or today's cure today's build?"""
    hdr(5, "CROSS-CORRELATION -- which side leads, at what lag?")
    out = {}
    for p in PLANTS:
        b = duck().execute(f"""
            SELECT date AS d, itemCode AS gt, count(*) AS q FROM v_build
            WHERE stage=2 AND plant='{p}' AND itemCode IS NOT NULL
            GROUP BY 1,2""").pl()
        c = duck().execute(f"""
            SELECT date AS d, gt, count(*) AS q FROM cure_gt
            WHERE plant='{p}' GROUP BY 1,2""").pl()
        if not b.height or not c.height:
            continue
        # plant-level daily totals
        bt = b.group_by("d").agg(pl.col("q").sum()).sort("d")
        ct = c.group_by("d").agg(pl.col("q").sum()).sort("d")
        m = bt.rename({"q": "bq"}).join(ct.rename({"q": "cq"}), on="d",
                                        how="inner").sort("d")
        x = np.array(m["bq"], float); y = np.array(m["cq"], float)
        x = (x - x.mean()) / (x.std() or 1); y = (y - y.mean()) / (y.std() or 1)
        print(f"  {p}  ({len(x)} days)   corr(build_t, cure_t+k):")
        row = []
        for k in range(-3, 4):
            if k >= 0:
                v = float(np.corrcoef(x[:len(x)-k], y[k:])[0, 1]) if k < len(x) else 0
            else:
                v = float(np.corrcoef(x[-k:], y[:len(y)+k])[0, 1])
            row.append((k, v))
        print("      " + "  ".join(f"k={k:+d} {v:+.2f}" for k, v in row))
        best = max(row, key=lambda t: abs(t[1]))
        out[p] = {"best_lag": best[0], "r": best[1]}
        lead = ("building leads curing" if best[0] > 0 else
                "curing leads building" if best[0] < 0 else "same-day coupling")
        print(f"      strongest at k={best[0]:+d} (r={best[1]:+.2f}) -> {lead}")
    print("\n  k>0 means building on day t predicts curing on day t+k")
    return out


# ---------------------------------------------------------------------------
def a6_inventory_profile() -> dict:
    """The GT ledger, from history. Level and stability are the tell."""
    hdr(6, "GT INVENTORY PROFILE -- historical WIP between the two stages")
    out = {}
    for p in PLANTS:
        d = duck().execute(f"""
            WITH e AS (
              SELECT date_trunc('day', event_ts) AS t, 1 AS dq FROM v_build
                WHERE stage=2 AND plant='{p}' AND itemCode IS NOT NULL
              UNION ALL
              SELECT date_trunc('day', event_ts) AS t, -1 AS dq FROM v_curing
                WHERE plant='{p}' AND gtbarCode IS NOT NULL)
            SELECT t, sum(dq) AS net FROM e GROUP BY t ORDER BY t""").pl()
        if not d.height:
            continue
        bal = np.cumsum(np.array(d["net"], float))
        # drop the first 14 days -- the ledger starts at an unknown opening stock
        bal = bal[14:] if len(bal) > 30 else bal
        lvl, sd = float(np.median(bal)), float(np.std(bal))
        rng = float(np.percentile(bal, 95) - np.percentile(bal, 5))
        out[p] = {"median": lvl, "sd": sd, "cv": sd / max(abs(lvl), 1), "p5_95": rng}
        print(f"  {p}:  median net WIP {lvl:>9,.0f}   sd {sd:>8,.0f}   "
              f"CV {sd/max(abs(lvl),1):>5.2f}   p5-p95 span {rng:>9,.0f}   "
              f"({len(bal)} days)")
    print("\n  NOTE: absolute level is relative to an unknown opening stock, so")
    print("        read the VARIABILITY (CV, span), not the level.")
    print("  INTERPRETATION: CV <0.3 => tightly controlled (pull) |"
          " CV >0.8 => accumulating (push)")
    return out


# ---------------------------------------------------------------------------
def a7_campaign_lifetime(camps: dict) -> dict:
    """Longer, more stable campaigns usually mark the master process."""
    hdr(7, "CAMPAIGN LIFETIME -- length and stability, each side")
    out = {}
    print(f"  {'plant':<6}{'side':<7}{'qty p50':>9}{'qty p90':>9}"
          f"{'hours p50':>11}{'CV(qty)':>9}{'spans >1 day':>14}")
    for p in PLANTS:
        for side in ["build", "cure"]:
            d = camps[(p, side)]
            if not d.height:
                continue
            hrs = ((d["t1"] - d["t0"]).dt.total_seconds() / 3600).to_numpy()
            q = np.array(d["qty"], float)
            multi = float(np.mean(hrs > 24))
            out[(p, side)] = {"p50": float(np.median(q)),
                              "cv": float(q.std() / max(q.mean(), 1e-9)),
                              "hrs": float(np.median(hrs))}
            print(f"  {p:<6}{side:<7}{np.median(q):>9,.0f}"
                  f"{np.percentile(q,90):>9,.0f}{np.median(hrs):>11.1f}"
                  f"{q.std()/max(q.mean(),1e-9):>9.2f}{100*multi:>13.0f}%")
    return out


# ---------------------------------------------------------------------------
def a8_bottleneck() -> dict:
    """Which resource class is saturated? The bottleneck shapes the plan."""
    hdr(8, "CONSTRAINT ANALYSIS -- which resource is actually saturated?")
    out = {}
    for p in PLANTS:
        d = duck().execute(f"""
            SELECT 'build' AS side, date AS d, machineCode AS res, count(*) AS n
              FROM v_build WHERE stage=2 AND plant='{p}' AND machineCode IS NOT NULL
              GROUP BY 1,2,3
            UNION ALL
            SELECT 'cure', date, CAST(wcID AS VARCHAR), count(*)
              FROM v_curing WHERE plant='{p}' AND wcID IS NOT NULL
              GROUP BY 1,2,3""").pl()
        if not d.height:
            continue
        for side in ["build", "cure"]:
            s = d.filter(pl.col("side") == side)
            if not s.height:
                continue
            days = s["d"].n_unique()
            res = s["res"].n_unique()
            # active-resource-days / possible-resource-days = occupancy
            occ = s.height / max(days * res, 1)
            per = s.group_by("res").agg(pl.col("d").n_unique().alias("dd"))
            out[(p, side)] = occ
            print(f"  {p:<5}{side:<7}{res:>4} resources over {days:>4} days   "
                  f"resource-day occupancy {100*occ:>5.1f}%   "
                  f"days active per resource p50 {float(per['dd'].median()):>5.0f}")
    for p in PLANTS:
        if (p, "build") in out and (p, "cure") in out:
            b, c = out[(p, "build")], out[(p, "cure")]
            who = ("CURING (presses)" if c > b + 0.05 else
                   "BUILDING (machines)" if b > c + 0.05 else "BALANCED")
            print(f"  -> {p}: build {100*b:.1f}% vs cure {100*c:.1f}% "
                  f"occupancy  |  tighter side: {who}")
    return out


# ---------------------------------------------------------------------------
def a9_dependency() -> dict:
    """Was the GT already there when the press wanted it?"""
    hdr(9, "BUILD-TO-CURE DEPENDENCY -- proactive or reactive building?")
    q = duck().execute("""
        SELECT b.plant,
               count(*) AS n,
               avg(CASE WHEN date_diff('second', b.event_ts, c.event_ts) < 0
                        THEN 1.0 ELSE 0.0 END) AS built_after,
               avg(CASE WHEN date_diff('second', b.event_ts, c.event_ts)
                        BETWEEN 0 AND 3600 THEN 1.0 ELSE 0.0 END) AS within_1h,
               avg(CASE WHEN date_diff('second', b.event_ts, c.event_ts)
                        > 172800 THEN 1.0 ELSE 0.0 END) AS over_48h,
               avg(CASE WHEN date_diff('second', b.event_ts, c.event_ts)
                        > 259200 THEN 1.0 ELSE 0.0 END) AS over_72h
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.productionID IS NOT NULL
        GROUP BY 1""").pl()
    out = {}
    print(f"  {'plant':<6}{'tyres':>11}{'cured before built':>20}"
          f"{'within 1 h':>12}{'>48 h':>9}{'>72 h':>9}")
    for r in q.iter_rows(named=True):
        out[r["plant"]] = dict(r)
        print(f"  {r['plant']:<6}{r['n']:>11,}{100*r['built_after']:>19.2f}%"
              f"{100*r['within_1h']:>11.1f}%{100*r['over_48h']:>8.1f}%"
              f"{100*r['over_72h']:>8.1f}%")
    print("\n  'within 1 h' high => presses waiting on building (REACTIVE)")
    print("  '>48 h' high      => building runs well ahead (PROACTIVE)")
    return out


# ---------------------------------------------------------------------------
def a10_sister_together(camps: dict) -> dict:
    """Are same-size GTs built back-to-back, or interleaved?"""
    hdr(10, "SISTER-SKU ADJACENCY -- are related GTs run together?")
    out = {}
    for p in PLANTS:
        d = camps[(p, "build")].sort(["res", "t0"])
        if d.height < 10:
            continue
        gts = d["gt"].to_list(); res = d["res"].to_list()

        def size_of(g: str) -> str:
            import re
            m = re.search(r"(\d+\.?\d*)\s*[/R]", g or "")
            return m.group(1) if m else (g or "")[:4]
        same = tot = 0
        for i in range(1, len(gts)):
            if res[i] != res[i - 1]:
                continue
            tot += 1
            if size_of(gts[i]) == size_of(gts[i - 1]):
                same += 1
        # baseline: chance adjacency if the same campaigns were shuffled
        sizes = [size_of(g) for g in gts]
        _, cnt = np.unique(sizes, return_counts=True)
        pr = float((cnt / cnt.sum() @ (cnt / cnt.sum())))
        out[p] = {"observed": same / max(tot, 1), "chance": pr}
        lift = (same / max(tot, 1)) / max(pr, 1e-9)
        print(f"  {p}:  consecutive campaigns sharing a size  "
              f"{100*same/max(tot,1):>5.1f}%   chance {100*pr:>5.1f}%   "
              f"lift {lift:>4.1f}x   ({tot:,} adjacencies)")
    print("\n  lift >2 => size/sister grouping is a deliberate policy")
    return out


# ---------------------------------------------------------------------------
def verdict(r: dict) -> None:
    hdr(11, "VERDICT -- what operational behaviour does the data demonstrate?")
    for p in PLANTS:
        votes = []
        lead = r["lead"].get(p, {})
        if lead:
            votes.append(("lead time", lead.get("verdict", "?")))
        c4 = r["chg"]
        if (p, "build") in c4 and (p, "cure") in c4:
            b, c = c4[(p, "build")]["per_day"], c4[(p, "cure")]["per_day"]
            votes.append(("changeover rate",
                          "BUILDING-DRIVEN" if b < c * 0.8 else
                          "CURING-DRIVEN / JIT" if c < b * 0.8 else "HYBRID"))
        x = r["xcorr"].get(p, {})
        if x:
            votes.append(("cross-correlation",
                          "BUILDING-DRIVEN" if x["best_lag"] > 0 else
                          "CURING-DRIVEN / JIT" if x["best_lag"] < 0 else "HYBRID"))
        inv = r["inv"].get(p, {})
        if inv:
            votes.append(("WIP stability",
                          "CURING-DRIVEN / JIT" if inv["cv"] < 0.3 else
                          "BUILDING-DRIVEN" if inv["cv"] > 0.8 else "HYBRID"))
        dep = r["dep"].get(p, {})
        if dep:
            votes.append(("dependency",
                          "BUILDING-DRIVEN" if dep["over_48h"] > 0.4 else
                          "CURING-DRIVEN / JIT" if dep["within_1h"] > 0.3
                          else "HYBRID"))
        print(f"\n  --- {p} ---")
        for name, v in votes:
            print(f"    {name:<20} -> {v}")
        tally: dict[str, int] = {}
        for _n, v in votes:
            tally[v] = tally.get(v, 0) + 1
        top = max(tally.items(), key=lambda t: t[1]) if tally else ("?", 0)
        agree = top[1] / max(len(votes), 1)
        print(f"    {'MAJORITY':<20} => {top[0]}  "
              f"({top[1]}/{len(votes)} analyses agree)")
        if agree < 0.6:
            print("    *** SPLIT VERDICT -- the analyses disagree. That is a "
                  "finding about the plant, not noise. ***")


def main() -> None:
    set_cutoff(None)
    print(SEP)
    print("PHASE 0 -- PLANT PLANNING-STRATEGY DIAGNOSIS (8 months MES history)")
    print(SEP)
    r: dict = {}
    r["lead"] = a1_lead_lag()
    # v_curing has no itemCode; the GT is reachable only via the barcode join.
    # Materialise once (~4 M rows) rather than re-joining 32 M rows per analysis.
    duck().execute("""
        CREATE OR REPLACE TABLE cure_gt AS
        SELECT c.plant, CAST(c.wcID AS VARCHAR) AS res, b.itemCode AS gt,
               c.event_ts, c.date
        FROM v_curing c JOIN v_build b ON c.gtbarCode = b.productionID
        WHERE b.stage = 2 AND b.itemCode IS NOT NULL AND c.wcID IS NOT NULL""")
    n = duck().execute("SELECT count(*) FROM cure_gt").fetchone()[0]
    print(f"\n  [cure_gt materialised: {n:,} cure events with a resolved GT]")
    camps = {}
    for p in PLANTS:
        for side in ["build", "cure"]:
            camps[(p, side)] = _campaigns(p, side)
    r["camp"] = a2_campaign_correlation(camps)
    r["stick"] = a3_stickiness(camps)
    r["chg"] = a4_changeovers(camps)
    r["xcorr"] = a5_cross_correlation()
    r["inv"] = a6_inventory_profile()
    r["life"] = a7_campaign_lifetime(camps)
    r["neck"] = a8_bottleneck()
    r["dep"] = a9_dependency()
    r["sis"] = a10_sister_together(camps)
    verdict(r)


if __name__ == "__main__":
    main()
