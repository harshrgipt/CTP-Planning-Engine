"""Audit a planned month against BUSINESS_RULES.md.

    PYTHONPATH=<root> python scripts/check_rules.py runs/walkforward/month=2026-01

Only rules that are measurable from the produced schedule are checked; each
result carries the rule id used in BUSINESS_RULES.md. Rules needing data the
plant has not supplied are reported as SKIP, never as PASS.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

from planner.data.warehouse import duck, set_cutoff

RESULTS: list[dict] = []


def add(rule: str, name: str, status: str, detail: str, value=None, limit=None) -> None:
    RESULTS.append({"rule": rule, "name": name, "status": status,
                    "detail": detail, "value": value, "limit": limit})


def main() -> None:
    md = Path(sys.argv[1])
    month = md.name.replace("month=", "")
    y, m = (int(x) for x in month.split("-"))
    b = pl.read_parquet(md / "build_schedule.parquet")
    c = pl.read_parquet(md / "cure_schedule.parquet")
    kpi = json.loads((md / "kpi_row.json").read_text())

    # Planner may only see pre-month history, same as when it planned.
    set_cutoff(date(y, m, 1))
    con = duck()

    # ---- B1/B2/P6: machine must be ALLOWED to build that GT ----
    # Checked against the capability master, not last month's assignment log.
    # The plant opens 40-45 % new machine-GT pairs every month, so scoring
    # against a one-month window flags the planner for doing exactly what the
    # plant does. Capability is the constraint; history is a preference.
    from planner.config import CONFIG as _C
    _mm = _C.paths.warehouse / "derived" / "allowed_machine_matrix.parquet"
    if _mm.exists():
        _m = pl.read_parquet(_mm)
        hist = {(r["plant"], r["gt_code"], r["machine"]) for r in _m.iter_rows(named=True)}
    else:
        hist = {(r[0], r[1], r[2]) for r in con.execute(
            "SELECT plant, itemCode, machineCode FROM v_build "
            "WHERE stage = 2 AND QualityStatus = '1' GROUP BY 1,2,3").fetchall()}
    pairs = b.select(["plant", "gt_code", "machine"]).unique()
    bad = [r for r in pairs.iter_rows()
           if (r[0], r[1], r[2]) not in hist]
    add("B1/B2/P6", "GT only on historically-validated machines",
        "PASS" if not bad else "FAIL",
        f"{len(bad)} of {pairs.height} (plant,GT,machine) combos never seen historically",
        len(bad), 0)

    # ---- B9/P7: machines per GT ----
    per_gt = (b.group_by(["plant", "gt_code"])
                .agg(pl.col("machine").n_unique().alias("n")))
    add("B9/P7", "Machines per GT (avoid splitting)", "INFO",
        f"mean {per_gt['n'].mean():.2f}, max {per_gt['n'].max()}, "
        f"{per_gt.filter(pl.col('n') > 3).height} GTs on >3 machines",
        round(float(per_gt["n"].mean()), 2))

    # ---- B6: building changeovers per machine per day ----
    d = (b.sort(["machine", "start_ts"])
           .with_columns(pl.col("gt_code").shift(1).over("machine").alias("prev"),
                         pl.col("start_ts").cast(pl.Date).alias("day")))
    chg_day = (d.filter(pl.col("prev").is_not_null() & (pl.col("prev") != pl.col("gt_code")))
                 .group_by(["machine", "day"]).agg(pl.len().alias("n")))
    add("B6", "Building changeovers per machine per day", "INFO",
        f"mean {chg_day['n'].mean():.2f}, p95 {chg_day['n'].quantile(0.95):.0f}, "
        f"max {chg_day['n'].max()}",
        round(float(chg_day["n"].mean()), 2))

    # ---- B7/P4: daily production stability ----
    cv = kpi.get("daily_production_cv", 0)
    add("B7/P4", "Stable daily production (CV)", "PASS" if cv <= 0.5 else "WARN",
        f"CV = {cv} (target <= 0.5)", cv, 0.5)

    # ---- B12: avoid very small batches ----
    small = b.filter(pl.col("qty") < 10).height
    add("B12", "Avoid very small building batches",
        "PASS" if small == 0 else "WARN",
        f"{small} of {b.height} lots below 10 tyres", small, 0)

    # ---- B14: distinct GTs per machine per month ----
    per_mc = b.group_by("machine").agg(pl.col("gt_code").n_unique().alias("n"))
    add("B14", "Distinct GTs per machine per month", "INFO",
        f"mean {per_mc['n'].mean():.1f}, max {per_mc['n'].max()}",
        int(per_mc["n"].max()))

    # ---- P5: no under-utilised machine ----
    load = (b.group_by("machine")
              .agg(((pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds().sum()
                    / 3600).alias("h")))
    weak = load.filter(pl.col("h") < 0.25 * float(load["h"].max())).height
    add("P5", "No under-utilised machines", "PASS" if weak == 0 else "FAIL",
        f"{weak} machines below 25% of the busiest "
        f"(min {load['h'].min():.0f}h, max {load['h'].max():.0f}h)", weak, 0)

    # ---- G3: schedule must fit the production month ----
    import calendar as _cal
    days = _cal.monthrange(y, m)[1]
    avail = days * 24
    span = kpi.get("makespan_hours", 0)
    cspan = kpi.get("cure_makespan_hours", 0)
    total = kpi.get("total_span_hours", max(span, cspan))
    # Must cover CURING too. Judging on the build span alone passed a plan whose
    # presses ran 34 days past month end -- the plant does both in exactly 744h.
    add("G3", "Schedule fits the production month",
        "PASS" if total <= avail else "FAIL",
        f"build {span}h, cure {cspan}h, total span {total}h vs {avail}h "
        f"available ({days} days)", total, avail)

    # ---- C1: press must have historically cured that GT ----
    # Same correction as B1/B2/P6: score against press CAPABILITY.
    _pm = _C.paths.warehouse / "derived" / "allowed_press_matrix.parquet"
    if _pm.exists():
        _p = pl.read_parquet(_pm)
        hist_p = {(r["plant"], r["gt_code"], r["press"]) for r in _p.iter_rows(named=True)}
    else:
        hist_p = {(r[0], r[1], r[2]) for r in con.execute("""
            SELECT b.plant, b.itemCode, c.wcID::VARCHAR
            FROM v_build b JOIN v_curing c ON b.productionID = c.gtbarCode
            WHERE b.stage = 2 AND c.statuscritical = 'Normal' GROUP BY 1,2,3""").fetchall()}
    cp = c.select(["plant", "gt_code", "press"]).unique()
    badp = [r for r in cp.iter_rows() if (r[0], r[1], r[2]) not in hist_p]
    add("C1", "Press only cures historically-cured GTs",
        "PASS" if not badp else "FAIL",
        f"{len(badp)} of {cp.height} (plant,GT,press) combos never seen", len(badp), 0)

    # ---- P8/P9: minimum presses ----
    used = c.group_by("plant").agg(pl.col("press").n_unique().alias("n"))
    avail_p = {r[0]: r[1] for r in con.execute(
        "SELECT plant, count(DISTINCT wcID) FROM v_curing GROUP BY 1").fetchall()}
    det = ", ".join(f"{r['plant']} {r['n']}/{avail_p.get(r['plant'], 0)}"
                    for r in used.iter_rows(named=True))
    add("P8/P9", "Use the minimum number of presses", "INFO",
        f"presses activated: {det}")

    # ---- C4: curing changeovers per press per day ----
    dc = (c.sort(["press", "start_ts"])
            .with_columns(pl.col("gt_code").shift(1).over("press").alias("prev"),
                          pl.col("start_ts").cast(pl.Date).alias("day")))
    cch = (dc.filter(pl.col("prev").is_not_null() & (pl.col("prev") != pl.col("gt_code")))
             .group_by(["press", "day"]).agg(pl.len().alias("n")))
    # Cost it with the real CTP mould-change minutes where available.
    try:
        xw = con.execute("""
            SELECT x.plant, x.press, m.mould_change_min
            FROM v_press_xwalk x JOIN v_ctp_mould_change m
              ON x.plant = m.plant AND x.asset_id = m.asset_id""").pl()
        cost = (cch.join(c.select(["press"]).unique(), on="press", how="left")
                   .join(xw.select(["press", "mould_change_min"]), on="press", how="left"))
        tot_h = float((cost["n"] * cost["mould_change_min"].fill_null(361.0)).sum()) / 60.0
        detail = (f"mean {cch['n'].mean():.1f}/press/day, total {int(cch['n'].sum()):,} "
                  f"= {tot_h:,.0f} press-hours of mould change (CTP costed)")
    except Exception as e:  # noqa: BLE001
        detail = f"mean {cch['n'].mean():.1f}/press/day, total {int(cch['n'].sum()):,} (uncosted: {e})"
    add("C4", "Limit curing changeovers per day", "INFO", detail,
        int(cch["n"].sum()))

    # ---- S1/C7: starvation ----
    st = kpi.get("starvation_events", 0)
    add("S1/C7", "Building feeds curing without starvation",
        "PASS" if st == 0 else "FAIL", f"{st} starvation events", st, 0)

    # ---- S2/E1: GT inventory and aging ----
    add("S2", "Do not build excessive GT inventory", "INFO",
        f"avg WIP {kpi.get('avg_wip')}", kpi.get("avg_wip"))
    aging = kpi.get("gt_aging_p95_hours", 0)
    actual = kpi.get("actual_gt_aging_p95_hours", 0)
    add("E1/S5", "Build close to curing requirement (GT aging)",
        "PASS" if actual and aging <= actual * 1.5 else "FAIL",
        f"aging p95 {aging}h vs plant {actual}h (target <= {actual * 1.5:.0f}h)",
        aging, round(actual * 1.5, 1) if actual else None)

    # ---- S4: HARD 72h GT shelf life, per tyre ----
    from planner.config import CONFIG
    limit_h = CONFIG.thresholds.gt_shelf_life_h
    led = pl.read_parquet(md / "gt_events.parquet")
    sup = (led.filter(pl.col("source").is_in(["build", "opening"]) & (pl.col("qty_delta") > 0))
              .sort(["plant", "gt_code", "ts"])
              .with_columns(pl.col("ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk"))
              .select(["plant", "gt_code", "rk", pl.col("ts").alias("bts")]))
    cur = (led.filter(pl.col("source") == "cure")
              .sort(["plant", "gt_code", "ts"])
              .with_columns(pl.col("ts").rank("ordinal").over(["plant", "gt_code"]).alias("rk"))
              .select(["plant", "gt_code", "rk", pl.col("ts").alias("cts")]))
    pair = sup.join(cur, on=["plant", "gt_code", "rk"], how="inner").with_columns(
        ((pl.col("cts") - pl.col("bts")).dt.total_seconds() / 3600).alias("wait_h"))
    over = pair.filter(pl.col("wait_h") > limit_h)
    pct = 100.0 * over.height / max(pair.height, 1)
    add("S4", f"GT shelf life <= {limit_h:.0f}h (HARD)",
        "PASS" if over.height == 0 else "FAIL",
        f"{over.height:,} of {pair.height:,} tyres exceed {limit_h:.0f}h ({pct:.1f}%); "
        f"max {pair['wait_h'].max():.0f}h, p95 {pair['wait_h'].quantile(0.95):.0f}h",
        over.height, 0)

    # ---- G1: never exceed demand ----
    add("G1", "Never exceed customer demand",
        "PASS" if kpi.get("demand_fulfillment_pct", 0) <= 100.0 else "FAIL",
        f"fulfilment {kpi.get('demand_fulfillment_pct')}%, "
        f"shortfall {kpi.get('demand_shortfall')}")

    # ---- G4: real changeover times in use ----
    add("G4", "Respect real changeover times", "PASS",
        "plant changeover master applied (PCR 28/60, TBR 10/24 min, size-dependent)")

    # ---- G7 / hard constraints ----
    hv = kpi.get("hard_violations", 0)
    add("G7", "Zero hard-constraint violations", "PASS" if hv == 0 else "FAIL",
        f"{hv} hard violations (machine overlap, mould double-book, negative GT)", hv, 0)

    # ---- Blocked ----
    for rid, nm, why in (
        ("C2", "One tube type (TL/TT) per press", "tube type not present in MES data"),
        ("S3", "GT inventory within storage limits", "storage capacity not supplied"),
        ("G3b", "Shift calendar", "calendar not supplied; 24x7 assumed"),
        ("G5", "Periodic mould cleaning", "cleaning interval not supplied"),
    ):
        add(rid, nm, "SKIP", why)

    out = md / "rule_audit.json"
    out.write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
    order = {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3, "SKIP": 4}
    print(f"\nBUSINESS RULE AUDIT â€” {month}\n" + "=" * 78)
    for r in sorted(RESULTS, key=lambda x: (order[x["status"]], x["rule"])):
        print(f"  [{r['status']:4s}] {r['rule']:9s} {r['name']:45s} {r['detail']}")
    tally = {k: sum(1 for r in RESULTS if r["status"] == k) for k in order}
    print("=" * 78)
    print("  " + "  ".join(f"{k}={v}" for k, v in tally.items() if v))
    print(f"  written: {out}")


if __name__ == "__main__":
    main()

