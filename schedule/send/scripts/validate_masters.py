"""Audit every master file we generated. Are they actually correct?

    python scripts/validate_masters.py

A master shared without validation gets used as if it were exact. This checks
referential integrity, cross-plant leakage, coverage against real demand, and
value sanity -- and prints FAIL/WARN/OK per check so the caveats are evidence,
not opinion.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

from planner.config import CONFIG
from planner.data.warehouse import duck, set_cutoff
from planner.runs.logger import log

FAIL, WARN, OK = [], [], []


def chk(cond: bool, msg: str, hard: bool = True) -> None:
    if cond:
        OK.append(msg)
        print(f"  OK   {msg}")
    elif hard:
        FAIL.append(msg)
        print(f"  FAIL {msg}")
    else:
        WARN.append(msg)
        print(f"  WARN {msg}")


def main() -> int:
    set_cutoff(None)
    con = duck()
    der = CONFIG.paths.warehouse / "derived"

    # ground truth from MES
    real_press: dict[str, set[str]] = {}
    for p, w in con.execute(
            "SELECT DISTINCT plant, wcID::VARCHAR FROM v_curing").fetchall():
        real_press.setdefault(p, set()).add(w)
    real_mach: dict[str, set[str]] = {}
    for p, m in con.execute(
            "SELECT DISTINCT plant, machineCode FROM v_build WHERE stage=2"
    ).fetchall():
        if m:
            real_mach.setdefault(p, set()).add(m)
    real_gt = {r[0] for r in con.execute(
        "SELECT DISTINCT itemCode FROM v_build WHERE stage=2 AND itemCode IS NOT NULL"
    ).fetchall()}

    print("=" * 74)
    print("1. ALLOWABLE PRESS MATRIX")
    print("=" * 74)
    ap = pl.read_parquet(der / "allowed_press_matrix.parquet")
    chk(ap.height > 0, f"file non-empty ({ap.height} rows)")
    chk(ap.select(["plant", "gt_code", "press"]).is_duplicated().sum() == 0,
        "no duplicate (plant, gt, press) rows")
    leak = []
    for p in sorted(real_press):
        got = set(ap.filter(pl.col("plant") == p)["press"].unique().to_list())
        bad = got - real_press[p]
        if bad:
            leak.append((p, len(got), len(real_press[p]), len(bad)))
    for p, got, real, nbad in leak:
        print(f"       {p}: lists {got} presses, {real} exist, {nbad} FOREIGN")
    chk(not leak, "no cross-plant press IDs")
    ug = set(ap["gt_code"].unique().to_list())
    chk(len(ug - real_gt) == 0,
        f"all {len(ug)} GT codes exist in MES ({len(ug - real_gt)} unknown)")
    per = ap.group_by(["plant", "gt_code"]).len()
    chk(int(per["len"].min()) >= 1, "every GT has >= 1 press")
    print(f"       presses per GT: p50={per['len'].median():.0f} "
          f"min={per['len'].min()} max={per['len'].max()}")

    print("\n" + "=" * 74)
    print("2. ALLOWABLE MACHINE MATRIX")
    print("=" * 74)
    am = pl.read_parquet(der / "allowed_machine_matrix.parquet")
    chk(am.height > 0, f"file non-empty ({am.height} rows)")
    chk(am.select(["plant", "gt_code", "machine"]).is_duplicated().sum() == 0,
        "no duplicate (plant, gt, machine) rows")
    leak = []
    for p in sorted(real_mach):
        got = set(am.filter(pl.col("plant") == p)["machine"].unique().to_list())
        bad = got - real_mach[p]
        if bad:
            leak.append((p, len(got), len(real_mach[p]), len(bad)))
    for p, got, real, nbad in leak:
        print(f"       {p}: lists {got} machines, {real} exist, {nbad} FOREIGN")
    chk(not leak, "no cross-plant machine IDs")
    perm = am.group_by(["plant", "gt_code"]).len()
    print(f"       machines per GT: p50={perm['len'].median():.0f} "
          f"min={perm['len'].min()} max={perm['len'].max()}")
    chk(float(perm["len"].median()) >= 3,
        f"median GT has >= 3 machine options (has {perm['len'].median():.0f})",
        hard=False)

    print("\n" + "=" * 74)
    print("3. COVERAGE vs ACTUAL DEMAND (all 8 months)")
    print("=" * 74)
    dd = CONFIG.paths.masters / "demand"
    dem = pl.concat([pl.read_csv(p) for p in sorted(dd.glob("demand_2*.csv"))])
    g = dem.select(["plant", "gt_code"]).unique()
    for nm, mx in [("press", ap), ("machine", am)]:
        miss = g.join(mx.select(["plant", "gt_code"]).unique(),
                      on=["plant", "gt_code"], how="anti")
        chk(miss.height == 0,
            f"{nm} matrix covers all {g.height} demanded GTs "
            f"({miss.height} missing)")
        for r in miss.head(5).iter_rows(named=True):
            print(f"       MISSING: {r['plant']}  {r['gt_code']}")

    print("\n" + "=" * 74)
    print("4. LINE SPEED / CYCLE TIME")
    print("=" * 74)
    cb = pl.read_parquet(der / "cycle_time_building.parquet")
    cc = pl.read_parquet(der / "cycle_time_curing.parquet")
    chk(bool((cb["s_per_tyre"] > 0).all()), "building cadence all > 0")
    chk(bool((cc["s_per_tyre"] > 0).all()), "curing cadence all > 0")
    # Compare against the plant's own ACTIVE-DAY median, which is what the
    # engine now plans with. Comparing to the shift model instead just checks
    # our slot assumption against itself; measured across 8 months PCR runs
    # 144-158/active-day and TBR 38-44, so the old fixed 156/48 pair had TBR
    # sitting at its p95 -- a best-day figure, ~14% optimistic.
    act = con.execute("""
        WITH pd AS (SELECT plant, wcID::VARCHAR p, CAST(event_ts AS DATE) d,
                           count(*) n FROM v_curing
                    WHERE statuscritical='Normal' GROUP BY 1,2,3)
        SELECT plant, quantile_cont(n,0.5) p50 FROM pd GROUP BY 1
    """).pl()
    for r in act.iter_rows(named=True):
        p = r["plant"]
        sub = cc.filter(pl.col("plant") == p)
        if sub.height == 0:
            continue
        col = "capacity_per_day" if "capacity_per_day" in cc.columns else "s_per_tyre"
        cap = (float(sub[col].median()) if col == "capacity_per_day"
               else 3 * (28800 // float(sub[col].median())))
        chk(abs(cap - float(r["p50"])) <= float(r["p50"]) * 0.20,
            f"{p} master capacity {cap:.0f}/press-day within 20% of the "
            f"active-day median {float(r['p50']):.0f}")
    miss = set(real_press.get("PCR", set()) | real_press.get("TBR", set())) - \
        set(cc["press"].unique().to_list())
    chk(len(miss) == 0, f"cycle time known for every press ({len(miss)} missing)",
        hard=False)

    print("\n" + "=" * 74)
    print("5. DEMAND FILES")
    print("=" * 74)
    chk(bool((dem["qty"] % 1 == 0).all()), "all demand quantities are INTEGER")
    chk(bool((dem["qty"] > 0).all()), "all demand quantities > 0")
    chk(len(set(dem["gt_code"].unique().to_list()) - real_gt) == 0,
        "all demand GT codes exist in MES")
    by = dem.group_by("month").agg(pl.col("qty").sum(),
                                   pl.col("due_date").n_unique().alias("days"))
    print(by.sort("month"))
    chk(bool((by["days"] >= 28).all()), "every month has >= 28 demand days")

    print("\n" + "=" * 74)
    print("6. OPENING GT INVENTORY")
    print("=" * 74)
    od = CONFIG.paths.masters / "opening_gt"
    op = pl.concat([pl.read_csv(p) for p in sorted(od.glob("opening_gt_2*.csv"))])
    chk(bool((op["qty"] > 0).all()), "all opening quantities > 0")
    chk(bool((op["age_max_h"] <= 72).all()),
        f"no opening tyre exceeds the 72h shelf life "
        f"(max {op['age_max_h'].max():.1f}h)")
    tot = op.group_by(["as_of", "plant"]).agg(pl.col("qty").sum())
    print(tot.sort(["as_of", "plant"]))
    pcr = tot.filter(pl.col("plant") == "PCR")["qty"]
    chk(bool(((pcr > 3500) & (pcr < 6500)).all()),
        "PCR opening stock within the observed 3,500-6,500 envelope", hard=False)

    print("\n" + "=" * 74)
    print(f"RESULT: {len(FAIL)} FAIL, {len(WARN)} WARN, {len(OK)} OK")
    print("=" * 74)
    for x in FAIL:
        print(f"  FAIL: {x}")
    for x in WARN:
        print(f"  WARN: {x}")
    log.info("validate_masters.done", fail=len(FAIL), warn=len(WARN), ok=len(OK))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
