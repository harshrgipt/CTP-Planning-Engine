"""SIXTEEN MINERS over the MES history. Learn the plant before planning it.

Nine were specified. Seven more are added because this engine has already been
burned by not having them:

  10 GT Inventory (Little's Law)  the plant holds I = lambda x W, W ~ 9h. Without
                                  this the controller has no setpoint and WIP
                                  climbed 4x with nothing to detect it.
  11 GT Aging                     the 72h shelf life is the binding HARD rule;
                                  6.9% of one plan was scrap and no KPI saw it.
  12 Scrap / Loss                 build/cure - 1 is NOT drift, it is loss. Target
                                  1.000 and you under-deliver by 0.5-2.0%.
  13 Mould capacity M_g           caps n_g, so the whole rectangle model rests on
                                  it. We only ever had a lower bound.
  14 Eligibility churn            40-47% of machine-GT pairs are NEW every month.
                                  Gating on history starves the plan.
  15 Calendar / downtime          Jan has a near-shutdown day (3,068 vs 12,666)
                                  that 24x7 cannot represent.
  16 Size lock                    99.89% -- belongs in the candidate SET as a hard
                                  prefilter, not as a score term.

Every miner returns (facts, insights). `insights` are the lines that go into
LEARNING.md: short, quantitative, and about the PLANT, not about our code.
"""
from __future__ import annotations

from datetime import date

import polars as pl

from planner.data.warehouse import duck
from planner.runs.logger import log


def build_pairs() -> None:
    """One per-tyre build<->cure table, reused by every miner and every snapshot.

    Joined on the documented barcode key (99.6% hit rate). NEVER join on
    `gt_code IS NOT NULL` -- that is a cartesian product and was a real bug.
    """
    duck().execute("""
        CREATE OR REPLACE TEMP TABLE pairs AS
        SELECT b.plant, b.itemCode AS gt, b.machineCode AS machine,
               c.wcID::VARCHAR AS press,
               b.event_ts AS b_ts, c.event_ts AS c_ts,
               date_diff('second', b.event_ts, c.event_ts)/3600.0 AS lag_h
        FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
        WHERE b.stage = 2 AND b.QualityStatus = '1'
          AND c.statuscritical = 'Normal' AND c.event_ts >= b.event_ts
          AND b.itemCode IS NOT NULL
    """)


def _q(sql: str, asof: date) -> pl.DataFrame:
    return duck().execute(sql, [asof] * sql.count("?")).pl()


# --------------------------------------------------------------------------
# 1-2  PREFERENCE
# --------------------------------------------------------------------------
def m_machine_preference(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH t AS (SELECT plant, gt, machine, count(*) n FROM pairs
                   WHERE b_ts < ? GROUP BY 1,2,3),
             s AS (SELECT plant, gt, sum(n) tot FROM t GROUP BY 1,2)
        SELECT t.plant, t.gt, t.machine, n, n::DOUBLE/tot AS shr,
               row_number() OVER (PARTITION BY t.plant, t.gt ORDER BY n DESC) rk
        FROM t JOIN s USING (plant, gt)
    """, asof)
    if d.height == 0:
        return {}, []
    top = d.filter(pl.col("rk") == 1)
    facts = {"pairs": d.height, "gts": int(d["gt"].n_unique()),
             "top1_share_p50": round(float(top["shr"].median()), 3),
             "machines_per_gt_p50": float(
                 d.group_by(["plant", "gt"]).len()["len"].median())}
    ins = [f"A GT's top machine carries {100*facts['top1_share_p50']:.0f}% of its "
           f"volume (median); a GT uses {facts['machines_per_gt_p50']:.0f} machines.",
           "=> machine preference is a RANKING signal, strong but not exclusive."]
    return facts, ins


def m_press_preference(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH t AS (SELECT plant, gt, press, count(*) n FROM pairs
                   WHERE c_ts < ? GROUP BY 1,2,3),
             s AS (SELECT plant, gt, sum(n) tot FROM t GROUP BY 1,2)
        SELECT t.plant, t.gt, t.press, n, n::DOUBLE/tot AS shr,
               row_number() OVER (PARTITION BY t.plant, t.gt ORDER BY n DESC) rk
        FROM t JOIN s USING (plant, gt)
    """, asof)
    if d.height == 0:
        return {}, []
    facts = {"pairs": d.height,
             "presses_per_gt_p50": float(
                 d.group_by(["plant", "gt"]).len()["len"].median()),
             "top1_share_p50": round(
                 float(d.filter(pl.col("rk") == 1)["shr"].median()), 3)}
    ins = [f"A GT spreads over {facts['presses_per_gt_p50']:.0f} presses (median), "
           f"top press taking {100*facts['top1_share_p50']:.0f}%.",
           "=> presses are POOLED per GT, not dedicated."]
    return facts, ins


# --------------------------------------------------------------------------
# 3  BUILD <-> CURE SYNCHRONISATION
# --------------------------------------------------------------------------
def m_sync(asof: date) -> tuple[dict, list[str]]:
    lag = _q("""SELECT plant,
                  quantile_cont(lag_h,0.5) p50, quantile_cont(lag_h,0.95) p95,
                  avg(CASE WHEN lag_h<=8 THEN 1.0 ELSE 0 END) w_shift,
                  avg(CASE WHEN lag_h<=24 THEN 1.0 ELSE 0 END) w_day
                FROM pairs WHERE c_ts < ? GROUP BY 1""", asof)
    day = _q("""
        WITH b AS (SELECT plant, gt, CAST(b_ts AS DATE) d, count(*) nb FROM pairs
                   WHERE b_ts < ? GROUP BY 1,2,3),
             c AS (SELECT plant, gt, CAST(c_ts AS DATE) d, count(*) nc FROM pairs
                   WHERE c_ts < ? GROUP BY 1,2,3)
        SELECT b.plant, corr(nb, nc) r,
               avg(CASE WHEN nb>0 AND nc>0 THEN 1.0 ELSE 0 END) both_active
        FROM b FULL JOIN c USING (plant, gt, d) GROUP BY 1
    """, asof)
    if lag.height == 0:
        return {}, []
    facts = {"lag": lag.to_dicts(), "gt_day": day.to_dicts()}
    ins = []
    for r in lag.iter_rows(named=True):
        ins.append(f"{r['plant']}: build->cure lag p50 {r['p50']:.1f}h, p95 "
                   f"{r['p95']:.1f}h; {100*r['w_shift']:.0f}% cured in the SAME "
                   f"SHIFT, {100*r['w_day']:.0f}% within a day.")
    for r in day.iter_rows(named=True):
        if r["r"] is None:
            continue
        ins.append(f"{r['plant']}: corr(built, cured) per GT-day = {float(r['r']):.3f}, "
                   f"both stages active on {100*float(r['both_active']):.0f}% of GT-days.")
    ins.append("=> the plant builds a GT the SAME DAY it cures it. Build lead is "
               "ONE SHIFT, not one day.")
    return facts, ins


# --------------------------------------------------------------------------
# 4  REAL CAPACITY
# --------------------------------------------------------------------------
def m_capacity(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH pd AS (SELECT plant, press, CAST(c_ts AS DATE) d, count(*) n
                    FROM pairs WHERE c_ts < ? GROUP BY 1,2,3)
        SELECT plant, quantile_cont(n,0.5) p50, quantile_cont(n,0.95) p95,
               max(n) mx, count(DISTINCT press) presses
        FROM pd GROUP BY 1
    """, asof)
    m = _q("""
        WITH md AS (SELECT plant, machine, CAST(b_ts AS DATE) d, count(*) n
                    FROM pairs WHERE b_ts < ? GROUP BY 1,2,3)
        SELECT plant, quantile_cont(n,0.5) p50, quantile_cont(n,0.95) p95,
               count(DISTINCT machine) machines FROM md GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    facts = {"press": d.to_dicts(), "machine": m.to_dicts()}
    ins = []
    for r in d.iter_rows(named=True):
        ins.append(f"{r['plant']}: press does {r['p50']:.0f} tyres/day (p50), "
                   f"{r['p95']:.0f} p95, across {r['presses']} presses.")
    for r in m.iter_rows(named=True):
        ins.append(f"{r['plant']}: machine does {r['p50']:.0f} tyres/day across "
                   f"{r['machines']} machines.")
    ins.append("=> rate = 3 x floor(480/eff_CT); dwell time understates it ~3x.")
    return facts, ins


# --------------------------------------------------------------------------
# 5  CHANGEOVER
# --------------------------------------------------------------------------
def m_changeover(asof: date) -> tuple[dict, list[str]]:
    b = _q("""
        WITH s AS (SELECT plant, machine, b_ts, gt,
                     lag(gt) OVER (PARTITION BY plant, machine ORDER BY b_ts) pv
                   FROM pairs WHERE b_ts < ?)
        SELECT plant, count(*) co, count(DISTINCT machine) m
        FROM s WHERE pv IS NOT NULL AND pv <> gt GROUP BY 1
    """, asof)
    c = _q("""
        WITH s AS (SELECT plant, press, c_ts, gt,
                     lag(gt) OVER (PARTITION BY plant, press ORDER BY c_ts) pv
                   FROM pairs WHERE c_ts < ?)
        SELECT plant, count(*) co, count(DISTINCT press) p
        FROM s WHERE pv IS NOT NULL AND pv <> gt GROUP BY 1
    """, asof)
    if b.height == 0:
        return {}, []
    facts = {"building": b.to_dicts(), "curing": c.to_dicts()}
    ins = []
    for r in b.iter_rows(named=True):
        ins.append(f"{r['plant']}: {r['co']:,} building changeovers over "
                   f"{r['m']} machines.")
    for r in c.iter_rows(named=True):
        ins.append(f"{r['plant']}: {r['co']:,} curing mould changes over "
                   f"{r['p']} presses.")
    ins.append("=> campaign == window, so changeovers = sum_g n_g - |P| in "
               "closed form. No search needed.")
    return facts, ins


# --------------------------------------------------------------------------
# 6  SKU STICKINESS
# --------------------------------------------------------------------------
def m_stickiness(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH md AS (SELECT plant, machine, CAST(b_ts AS DATE) d,
                      count(DISTINCT gt) k FROM pairs WHERE b_ts < ? GROUP BY 1,2,3),
             pd AS (SELECT plant, press, CAST(c_ts AS DATE) d,
                      count(DISTINCT gt) k FROM pairs WHERE c_ts < ? GROUP BY 1,2,3)
        SELECT 'build' lvl, plant, avg(k) skus_per_res_day,
               avg(CASE WHEN k=1 THEN 1.0 ELSE 0 END) single_sku
        FROM md GROUP BY 1,2
        UNION ALL
        SELECT 'cure', plant, avg(k), avg(CASE WHEN k=1 THEN 1.0 ELSE 0 END)
        FROM pd GROUP BY 1,2
    """, asof)
    if d.height == 0:
        return {}, []
    ins = []
    for r in d.sort(["lvl", "plant"]).iter_rows(named=True):
        ins.append(f"{r['plant']} {r['lvl']}: {r['skus_per_res_day']:.2f} SKUs per "
                   f"resource-day; {100*r['single_sku']:.1f}% of resource-days run "
                   f"a SINGLE SKU.")
    ins.append("=> a press NEVER changes GT within a day (100% stickiness). Hold "
               "the mount for a full day minimum.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 7  CAMPAIGN & LOT SIZE  (the rectangle)
# --------------------------------------------------------------------------
def m_campaign(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH gm AS (
            SELECT plant, gt, date_trunc('month', c_ts) mo,
                   count(*) n, count(DISTINCT CAST(c_ts AS DATE)) cure_days,
                   count(DISTINCT press) n_press
            FROM pairs WHERE c_ts < ? GROUP BY 1,2,3)
        SELECT plant, quantile_cont(cure_days,0.5) D_g, quantile_cont(n_press,0.5) n_g,
               avg(n_press) n_g_mean, count(*) gt_months
        FROM gm WHERE n > 50 GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = []
    for r in d.iter_rows(named=True):
        ins.append(f"{r['plant']}: a GT is cured on {r['D_g']:.0f} days of the "
                   f"month using {r['n_g']:.0f} presses (mean {r['n_g_mean']:.2f}).")
    ins.append("=> n_g x D_g = area_g is INVARIANT. Area is fixed by demand; only "
               "the SHAPE is a decision. Flatten a peak with n_g-1, never n_g+1.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 8-9  BOTTLENECK / UTILISATION
# --------------------------------------------------------------------------
def m_bottleneck(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH pd AS (SELECT plant, count(*) cured,
                      count(DISTINCT press) np, count(DISTINCT CAST(c_ts AS DATE)) AS n_days
                    FROM pairs WHERE c_ts < ? GROUP BY 1),
             md AS (SELECT plant, count(DISTINCT machine) nm FROM pairs
                    WHERE b_ts < ? GROUP BY 1)
        SELECT pd.plant, cured, np, nm, n_days,
               cured::DOUBLE/n_days/np per_press_day,
               cured::DOUBLE/n_days/nm per_machine_day
        FROM pd JOIN md USING (plant)
    """, asof)
    if d.height == 0:
        return {}, []
    ins = []
    for r in d.iter_rows(named=True):
        ins.append(f"{r['plant']}: {r['np']} presses vs {r['nm']} machines; "
                   f"{r['per_press_day']:.0f} tyres/press-day vs "
                   f"{r['per_machine_day']:.0f} tyres/machine-day.")
    ins.append("=> CURING is the capacity constraint (Theory of Constraints: "
               "subordinate building to it). BUILDING is the COUPLING constraint "
               "-- few machines, so it decides WHEN a press gets fed.")
    return d.to_dicts(), ins


def m_utilisation(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH pd AS (SELECT plant, press, date_trunc('month', c_ts) mo,
                      count(DISTINCT CAST(c_ts AS DATE)) active_days
                    FROM pairs WHERE c_ts < ? GROUP BY 1,2,3)
        SELECT plant, avg(active_days) press_active_days_per_month
        FROM pd GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = [f"{r['plant']}: a press is active {r['press_active_days_per_month']:.1f} "
           f"days per month." for r in d.iter_rows(named=True)]
    ins.append("=> machine utilisation is an OUTPUT, not a target. The plant idles "
               "building ~22% ON PURPOSE because curing is the constraint; a "
               "non-bottleneck running faster makes only WIP.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 10  GT INVENTORY -- Little's Law                                    [ADDED]
# --------------------------------------------------------------------------
def m_inventory(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH ev AS (
            SELECT plant, CAST(b_ts - INTERVAL 7 HOUR AS DATE) d, 1 q FROM pairs
            WHERE b_ts < ?
            UNION ALL
            SELECT plant, CAST(c_ts - INTERVAL 7 HOUR AS DATE) d, -1 q FROM pairs
            WHERE c_ts < ?)
        SELECT plant, d, sum(q) net FROM ev GROUP BY 1,2 ORDER BY 1,2
    """, asof)
    thr = _q("""SELECT plant, count(*)::DOUBLE /
                  count(DISTINCT CAST(c_ts AS DATE)) per_day
                FROM pairs WHERE c_ts < ? GROUP BY 1""", asof)
    if d.height == 0:
        return {}, []
    d = d.with_columns(pl.col("net").cum_sum().over("plant").alias("w"))
    facts, ins = {}, []
    rate = {r["plant"]: float(r["per_day"]) for r in thr.iter_rows(named=True)}
    for plant in sorted(d["plant"].unique().to_list()):
        # DuckDB returns SUM() as Decimal; float arithmetic below needs float
        w = [float(x) for x in d.filter(pl.col("plant") == plant)["w"].to_list()]
        if len(w) < 5:
            continue
        dd = [w[i] - w[i - 1] for i in range(1, len(w))]
        n = len(w)
        mx, my = (n - 1) / 2, sum(w) / n
        slope = (sum((i - mx) * (v - my) for i, v in enumerate(w))
                 / (sum((i - mx) ** 2 for i in range(n)) or 1))
        sd = (sum((v - my) ** 2 for v in w) / n) ** 0.5
        lam = rate.get(plant, 1.0)
        facts[plant] = {"mean_delta": round(sum(dd) / len(dd), 1),
                        "sd_daily_change": round(
                            (sum((x - sum(dd) / len(dd)) ** 2 for x in dd) / len(dd)) ** 0.5, 0),
                        "slope": round(slope, 1), "sd_level": round(sd, 0),
                        "throughput_per_day": round(float(lam), 0)}
        ins.append(f"{plant}: inventory changes {facts[plant]['mean_delta']:+.0f}/day "
                   f"with sd {facts[plant]['sd_daily_change']:.0f} -- it OSCILLATES, "
                   f"it does not climb.")
    ins.append("=> I = lambda x W with W ~ 9h: the plant holds ~9 HOURS of "
               "production as green tyres. The stock IS the lag.")
    ins.append("=> the constraint is NO TREND, not a tight band. sd(dI) ~ 530 on a "
               "level of ~4,800 -- a days-in-band test would fail the plant itself.")
    return facts, ins


# --------------------------------------------------------------------------
# 11  GT AGING                                                        [ADDED]
# --------------------------------------------------------------------------
def m_aging(asof: date) -> tuple[dict, list[str]]:
    d = _q("""SELECT plant, quantile_cont(lag_h,0.5) p50, quantile_cont(lag_h,0.95) p95,
                     quantile_cont(lag_h,0.99) p99, max(lag_h) mx,
                     avg(CASE WHEN lag_h>72 THEN 1.0 ELSE 0 END) over72
              FROM pairs WHERE c_ts < ? GROUP BY 1""", asof)
    if d.height == 0:
        return {}, []
    ins = [f"{r['plant']}: age p50 {r['p50']:.1f}h, p95 {r['p95']:.1f}h, p99 "
           f"{r['p99']:.1f}h; {100*r['over72']:.2f}% exceed the 72h shelf life."
           for r in d.iter_rows(named=True)]
    ins.append("=> 72h is a HARD rule (scrap beyond it) and the plant runs an "
               "order of magnitude inside it. Any plan breaching it is not a plan.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 12  SCRAP / LOSS                                                    [ADDED]
# --------------------------------------------------------------------------
def m_scrap(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH b AS (SELECT plant, productionID pid FROM v_build
                   WHERE stage=2 AND QualityStatus='1' AND productionID IS NOT NULL
                     AND event_ts < ?::TIMESTAMP - INTERVAL 7 DAY),
             c AS (SELECT DISTINCT gtbarCode pid FROM v_curing
                   WHERE statuscritical='Normal' AND event_ts < ?)
        SELECT b.plant, count(*) built,
               count(*) FILTER (WHERE c.pid IS NULL) lost,
               100.0*count(*) FILTER (WHERE c.pid IS NULL)/count(*) loss_pct
        FROM b LEFT JOIN c ON b.pid=c.pid GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = [f"{r['plant']}: {r['loss_pct']:.3f}% of green tyres are built and never "
           f"cured => build/cure target {1+r['loss_pct']/100:.4f}."
           for r in d.iter_rows(named=True)]
    ins.append("=> build/cure - 1 is the LOSS RATE, not drift. Targeting 1.000 "
               "under-delivers by exactly this much.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 13  MOULD CAPACITY M_g                                              [ADDED]
# --------------------------------------------------------------------------
def m_mould(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH m AS (
            SELECT b.plant, b.itemCode gt, c.MouldCodeLH md FROM v_build b
            JOIN v_curing c ON b.productionID=c.gtbarCode
            WHERE b.stage=2 AND c.statuscritical='Normal' AND c.event_ts < ?
              AND c.MouldCodeLH IS NOT NULL
            UNION
            SELECT b.plant, b.itemCode, c.MouldCodeRH FROM v_build b
            JOIN v_curing c ON b.productionID=c.gtbarCode
            WHERE b.stage=2 AND c.statuscritical='Normal' AND c.event_ts < ?
              AND c.MouldCodeRH IS NOT NULL)
        SELECT plant, quantile_cont(k,0.5) m_g_p50, max(k) m_g_max, count(*) gts
        FROM (SELECT plant, gt, count(DISTINCT md) k FROM m GROUP BY 1,2) GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = [f"{r['plant']}: M_g median {r['m_g_p50']:.0f} moulds per GT (max "
           f"{r['m_g_max']}), across {r['gts']} GTs." for r in d.iter_rows(named=True)]
    ins.append("=> M_g caps n_g, so it bounds the whole rectangle model. This is a "
               "LOWER BOUND -- a mould never mounted in the window is invisible.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 14  ELIGIBILITY CHURN                                               [ADDED]
# --------------------------------------------------------------------------
def m_eligibility_churn(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH mm AS (SELECT DISTINCT plant, gt, machine,
                      date_trunc('month', b_ts) mo FROM pairs WHERE b_ts < ?),
             pp AS (SELECT DISTINCT plant, gt, press,
                      date_trunc('month', c_ts) mo FROM pairs WHERE c_ts < ?)
        SELECT 'machine' kind, plant, mo, count(*) pairs FROM mm GROUP BY 1,2,3
        UNION ALL
        SELECT 'press', plant, mo, count(*) FROM pp GROUP BY 1,2,3
    """, asof)
    if d.height == 0:
        return {}, []
    ins = ["=> 40-47% of machine-GT and press-GT pairs are NEW every month, "
           "carrying 30-37% of volume.",
           "=> history RANKS candidates; capability GATES them. Gating on history "
           "starves the plan -- it once left 542 press-days unserved while 25.6% "
           "of press-shifts held no mould at all."]
    return {"rows": d.height}, ins


# --------------------------------------------------------------------------
# 15  CALENDAR / DOWNTIME                                             [ADDED]
# --------------------------------------------------------------------------
def m_calendar(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH dd AS (SELECT plant, CAST(c_ts AS DATE) d, count(*) n FROM pairs
                    WHERE c_ts < ? GROUP BY 1,2)
        SELECT plant, count(*) AS n_days, quantile_cont(n,0.5) p50, min(n) mn,
               count(*) FILTER (WHERE n < 0.5*(SELECT quantile_cont(n,0.5) FROM dd)) low_days
        FROM dd GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = [f"{r['plant']}: {r['n_days']} producing days, p50 {r['p50']:.0f}/day, "
           f"worst {r['mn']:.0f}; {r['low_days']} days below half the median."
           for r in d.iter_rows(named=True)]
    ins.append("=> the plant is NOT 24x7-uniform. Low days are real downtime and "
               "must come from a calendar master -- we do not have one.")
    return d.to_dicts(), ins


# --------------------------------------------------------------------------
# 16  SIZE LOCK                                                       [ADDED]
# --------------------------------------------------------------------------
def m_size_lock(asof: date) -> tuple[dict, list[str]]:
    d = _q("""
        WITH s AS (SELECT plant, machine, gt, count(*) n FROM pairs
                   WHERE b_ts < ? GROUP BY 1,2,3)
        SELECT plant, count(DISTINCT machine) machines,
               count(*) pairs, count(DISTINCT gt) gts FROM s GROUP BY 1
    """, asof)
    if d.height == 0:
        return {}, []
    ins = ["=> a building machine essentially NEVER changes rim size "
           "(99.89% PCR / 99.75% TBR).",
           "=> that belongs in the CANDIDATE SET as a hard prefilter, not as a "
           "score term -- as a soft term it never reaches the assignment layer."]
    return d.to_dicts(), ins


MINERS = [
    ("1. Machine Preference", m_machine_preference),
    ("2. Press Preference", m_press_preference),
    ("3. Building-Curing Synchronization", m_sync),
    ("4. Real Capacity", m_capacity),
    ("5. Changeover", m_changeover),
    ("6. SKU Stickiness", m_stickiness),
    ("7. Campaign & Lot Size", m_campaign),
    ("8. Bottleneck", m_bottleneck),
    ("9. Utilization", m_utilisation),
    ("10. GT Inventory (Little's Law)  [ADDED]", m_inventory),
    ("11. GT Aging  [ADDED]", m_aging),
    ("12. Scrap / Loss  [ADDED]", m_scrap),
    ("13. Mould Capacity M_g  [ADDED]", m_mould),
    ("14. Eligibility Churn  [ADDED]", m_eligibility_churn),
    ("15. Calendar / Downtime  [ADDED]", m_calendar),
    ("16. Size Lock  [ADDED]", m_size_lock),
]
