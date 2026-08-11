"""L1 -- VALIDATION & CONSTRAINT COMPILATION.  Phase B entry.

    python -m planner.cmbc.l1_validate --month 2026-07

Three outputs:
  1. a VALIDATION REPORT  -- every input checked, every failure named
  2. `rule_table_<month>.parquet`  -- R1..R18 compiled to DATA, not code
  3. `cost_table.parquet`          -- the Decision Cost Engine, compile side

WHY RULES BECOME DATA
  A rule embedded in code can only be changed by a release. Compiled to a table
  with (id, class, tier, weight, expression, owner, data_source, status) the
  plant can retune weights without one, and every rule's backing data is
  auditable in the same row that states the rule.

SEVERITY
  ERROR   -- planning cannot proceed correctly; the plan would be wrong
  WARN    -- planning proceeds, but a rule degrades to soft or partial coverage
  INFO    -- observation worth surfacing, no action required

A check that cannot run because its data is missing reports as ERROR, never as
a silent pass. Absence of evidence is not evidence of validity.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import polars as pl

from planner.config import GT_SHELF_LIFE_H
from planner import paths

ROOT = paths.ROOT   # depth-independent; this file moved one level deeper
SRC = paths.RAW
D = ROOT / "warehouse" / "derived"
INP = paths.INPUT_DERIVED
OUT = ROOT / "warehouse" / "derived"

FINDINGS: list[dict] = []


def add(sev: str, check: str, detail: str, n: int = 0, rule: str = "") -> None:
    FINDINGS.append({"severity": sev, "check": check, "rule": rule,
                     "n": n, "detail": detail})


def _read(p: Path) -> pl.DataFrame | None:
    try:
        return pl.read_parquet(p) if p.exists() else None
    except Exception:                                          # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
def validate(month: str) -> tuple[pl.DataFrame, dict]:
    dem_f = ROOT / "masters" / "demand" / f"demand_{month}.parquet"
    if not dem_f.exists():
        add("ERROR", "demand.exists", f"demand_{month}.parquet not found", rule="R1")
        return pl.DataFrame(FINDINGS), {}
    dem = pl.read_parquet(dem_f)
    uni = {p: set(dem.filter(pl.col("plant") == p)["gt_code"].unique().to_list())
           for p in ["PCR", "TBR"]}
    ctx = {"demand": dem, "universe": uni}

    # ---- 1. demand integrity (R1) ---------------------------------------
    if dem.filter(pl.col("qty") <= 0).height:
        add("ERROR", "demand.positive", "rows with qty <= 0",
            dem.filter(pl.col("qty") <= 0).height, "R1")
    for c in ["plant", "gt_code", "sku", "qty"]:
        n = dem.filter(pl.col(c).is_null()).height
        if n:
            add("ERROR", f"demand.{c}.not_null", f"null {c}", n, "R1")
    if dem["month"].n_unique() > 1:
        add("ERROR", "demand.month.single",
            f"mixed months: {dem['month'].unique().to_list()}", rule="R1")
    add("INFO", "demand.size",
        f"{dem.height} rows · PCR {len(uni['PCR'])} GTs · TBR {len(uni['TBR'])} GTs",
        dem.height, "R1")

    # ---- 2. minimum demand floor (B12) ----------------------------------
    from planner.config import CONFIG
    md = CONFIG.thresholds.min_demand_units
    g = dem.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("q"))
    for p, floor in md.items():
        s = g.filter((pl.col("plant") == p) & (pl.col("q") < floor))
        if s.height:
            add("INFO", "demand.below_min",
                f"{p}: {s.height} GTs below min_demand {floor} "
                f"({int(s['q'].sum()):,} tyres) -> residual policy, not planned",
                s.height, "B12")

    # ---- 3. eligibility (R2) --------------------------------------------
    mm = _read(D / "allowed_machine_matrix.parquet")
    if mm is None:
        add("ERROR", "eligibility.machine", "allowed_machine_matrix missing", rule="R2")
    else:
        for p in ["PCR", "TBR"]:
            have = set(mm.filter(pl.col("plant") == p)["gt_code"].to_list())
            miss = uni[p] - have
            if miss:
                add("ERROR", "eligibility.machine.coverage",
                    f"{p}: {len(miss)} planned GTs have NO eligible machine "
                    f"e.g. {sorted(miss)[:2]}", len(miss), "R2")
    pm = _read(D / "allowed_press_matrix.parquet")
    if pm is None:
        add("ERROR", "eligibility.press", "allowed_press_matrix missing", rule="R3")
    else:
        for p in ["PCR", "TBR"]:
            have = set(pm.filter(pl.col("plant") == p)["gt_code"].to_list())
            miss = uni[p] - have
            if miss:
                add("ERROR", "eligibility.press.coverage",
                    f"{p}: {len(miss)} planned GTs have NO eligible press "
                    f"e.g. {sorted(miss)[:2]}", len(miss), "R3")

    # ---- 4. mould master (R3, R14) --------------------------------------
    mapf = paths.raw("curing_item_mould_mapping 2.csv")
    invf = paths.raw("mould_inv_ctp_17072026.csv")
    if not (mapf.exists() and invf.exists()):
        add("ERROR", "mould.master", "mould mapping or inventory missing", rule="R3")
    else:
        mp = pl.read_csv(mapf, infer_schema_length=0)
        inv = pl.read_csv(invf, infer_schema_length=0)
        act = set(inv.filter(pl.col("CurrStat") == "ACTIVE")["Equnr"].to_list())
        mp = mp.with_columns(pl.col("Mold_Name").is_in(list(act)).alias("a"))
        per = mp.group_by("Item_Code").agg(
            pl.col("a").sum().cast(pl.Int64).alias("active"))
        gs = dem.select(["plant", "gt_code", "sku"]).unique()
        gm = (gs.join(per, left_on="sku", right_on="Item_Code", how="left")
              .group_by(["plant", "gt_code"])
              .agg(pl.col("active").sum().cast(pl.Int64).alias("active")))
        # RECONCILE ON CURABILITY, NOT ON MOULD IDENTITY.
        #   MES  v_curing.mouldNo  : '-#GC02', '0', '0#0'
        #   master Mold_Name       : '17575R16RANHPHMI01'
        # These are different namespaces -- a short position/cavity suffix vs a
        # full SAP equipment code -- so counting distinct observed moulds and
        # comparing them to the master compares nothing (all 475 observed values
        # "miss" the master purely on string form). An earlier version of this
        # check did exactly that and raised a spurious "50 GTs stale" warning.
        #
        # The question that actually matters is identity-independent: was this
        # GT ever cured? If yes it is curable, whatever the mould is called.
        # Mould COUNT still comes from the master alone -- it is the only source
        # that carries it.
        from planner.data.warehouse import duck, set_cutoff
        set_cutoff(None)
        cured = duck().execute("""
            SELECT DISTINCT b.plant, b.itemCode AS gt_code
            FROM v_curing c JOIN v_build b ON c.gtbarCode = b.productionID
            WHERE b.stage = 2 AND b.itemCode IS NOT NULL""").pl()
        gm = (gm.join(cured.with_columns(pl.lit(True).alias("curable")),
                      on=["plant", "gt_code"], how="left")
              .with_columns(pl.col("active").fill_null(0),
                            pl.col("curable").fill_null(False))
              .rename({"active": "moulds"}))

        blind = gm.filter((pl.col("moulds") == 0) & pl.col("curable"))
        if blind.height:
            add("WARN", "mould.master_incomplete",
                f"{blind.height} GTs have no mould in the master but WERE cured "
                f"in MES (e.g. {blind['gt_code'].to_list()[:2]}) -- treated as "
                f"curable with unknown mould count; report to plant",
                blind.height, "R3")

        zero = gm.filter((pl.col("moulds") == 0) & ~pl.col("curable"))
        cav = _read(INP / "mould_cavity.parquet")
        if cav is None or cav.height == 0:
            add("ERROR", "mould.cavities",
                "cavity count unavailable (Full_Load 100% NULL) -- min_cure_lot "
                "cannot be derived; L4.5 falls back to fixed floors", rule="R3")

    # ---- 5. TT/TL (R8 / B16) --------------------------------------------
    tt = _read(INP / "tt_tl.parquet")
    if tt is None:
        add("ERROR", "tt_tl.exists", "tt_tl.parquet missing -- B16 cannot run",
            rule="B16")
    else:
        m = tt.filter(pl.col("sku") != "").select(["sku", "tt_tl"]).unique(
            subset=["sku"])
        j = dem.filter(pl.col("plant") == "TBR").join(m, on="sku", how="left")
        un = j.filter(pl.col("tt_tl").is_null())
        if un.height:
            add("ERROR", "tt_tl.coverage",
                f"TBR: {un['gt_code'].n_unique()} GTs untagged "
                f"({int(un['qty'].sum()):,} tyres) -- B16 cannot assign them",
                un.height, "B16")
        else:
            hrs = j.group_by("tt_tl").agg(pl.col("qty").sum().alias("q"))
            tot = float(hrs["q"].sum())
            ttq = float(hrs.filter(pl.col("tt_tl") == "TT")["q"].sum() or 0)
            n_tt = round(9 * ttq / tot) if tot else 0
            add("INFO", "tt_tl.split",
                f"TBR TT {100*ttq/tot:.1f}% -> {n_tt} TT machines / {9-n_tt} TL",
                9, "B16")
            ctx["tt_split"] = (n_tt, 9 - n_tt)

    # ---- 6. opening inventory (R1, R5) ----------------------------------
    # SAME OVERRIDE AS L4/L5/L7. Without it this layer validated the MES-derived
    # master while the plan ran on a different opening file (a manual plant count
    # or the previous month's carry-forward), and the pack then reported an
    # opening stock the schedule never saw.
    _ogd = ROOT / "masters" / "opening_gt"
    _ogov = os.environ.get("PLANNER_OPENING_GT", "").strip()
    og = (Path(_ogov) if (_ogov and Path(_ogov).is_absolute())
          else (_ogd / _ogov) if _ogov else _ogd / f"opening_gt_{month}.parquet")
    o = _read(og)
    if o is None:
        add("WARN", "inventory.opening",
            f"opening_gt_{month}.parquet missing -- R1 nets nothing", rule="R1")
    else:
        stale = o.filter(pl.col("age_h") > GT_SHELF_LIFE_H)
        if stale.height:
            add("ERROR", "inventory.age",
                f"{stale.height} opening tyres already past {GT_SHELF_LIFE_H:.0f} h "
                f"-- scrap, must not be counted as stock", stale.height, "R5")
        add("INFO", "inventory.opening",
            f"{o.height:,} tyres, max age {float(o['age_h'].max()):.1f} h",
            o.height, "R1")

    # ---- 7. capacity + calendar (R10) -----------------------------------
    for n, f, rule in [("capacity_machine_day", "capacity_machine_day", "R10"),
                       ("capacity_press_day", "capacity_press_day", "R10"),
                       ("calendar_shifts", "calendar_shifts", "R10"),
                       ("cycle_time_curing", "cycle_time_curing", "R3"),
                       ("cycle_time_building", "cycle_time_building", "R10")]:
        d = _read(D / f"{f}.parquet")
        if d is None or d.height == 0:
            add("ERROR", f"capacity.{n}", f"{f}.parquet missing or empty", rule=rule)

    # ---- 8. size / rim (R6, R7) -----------------------------------------
    sz = _read(INP / "gt_size.parquet")
    if sz is None:
        add("ERROR", "size.exists", "gt_size.parquet missing", rule="R6")
    else:
        have = set(sz["sku"].to_list())
        for p in ["PCR", "TBR"]:
            sk = set(dem.filter(pl.col("plant") == p)["sku"].unique().to_list())
            miss = sk - have
            if miss:
                add("WARN", "size.coverage",
                    f"{p}: {len(miss)}/{len(sk)} SKUs have no rim -- "
                    f"R6/R7 degrade to soft for those", len(miss), "R6")

    # ---- 9. L0 parameters present ---------------------------------------
    pj = sorted((ROOT / "warehouse" / "params").glob("params_*.json"))
    if not pj:
        add("ERROR", "params.l0", "no L0 parameter file -- run l0_learn first")
    else:
        add("INFO", "params.l0", f"using {pj[-1].name}")

    return pl.DataFrame(FINDINGS), ctx


# ---------------------------------------------------------------------------
RULES = [
    # id, text, class, tier, weight, layer, data_source, status
    ("R1", "Demand & inventory netting", "hard", 1, None, "L4",
     "demand + opening_gt", "enforced"),
    ("R2", "SKU-machine eligibility", "soft-high", 8, 10000, "L2/L6",
     "allowed_machine_matrix + pcr_inch_eligibility", "priced"),
    ("R3", "Mould-based quantity", "hard", 3, None, "L3/L5",
     "mould_inv + curing_item_mould_mapping", "partial: no cavity count"),
    ("R4", "Curing capacity alignment", "hard", 4, None, "L5",
     "capacity_press_day + cycle_time_curing", "enforced"),
    ("R5", "GT age <= 72 h, FEFO", "hard", 2, None, "L7/L11",
     "GT_SHELF_LIFE_H (hardcoded 72)", "enforced"),
    ("R6", "Same SKU / same inch", "soft", 9, 500, "L6",
     "gt_size", "cure-high, build-low"),
    ("R7", "Minimum changeover", "soft", 6, None, "L9",
     "Master_Building_ChangeoverTime + build_setup_catalogue", "enforced"),
    ("R8", "TT/TL separation", "hard", 3, None, "L2/L6",
     "tt_tl", "see B16"),
    ("R9", "Campaign / batch minimums", "soft", 3, 2000, "L4.5",
     "lot_size + observed_lot_sizes", "fixed floors"),
    ("R10", "Capacity & availability", "hard", 4, None, "L6",
     "capacity_* + press availability (L0)", "enforced"),
    ("R11", "Exception replanning", "trigger", None, None, "L13", "-", "open"),
    ("R12", "Plan validation gate", "hard", 1, None, "L11", "-", "enforced"),
    ("R13", "Mould-change crew capacity", "hard", 3, None, "L10",
     "Master_Building_ChangeoverTime (crew) + press_mould_change", "open"),
    ("R14", "Mould<->press compatibility", "soft", 8, 200, "L2",
     "press_platen_master + observed pairs", "partial"),
    ("R15", "Yield / scrap grossing-up", "hard", 1, None, "L4",
     "L0 yields (cure only; build has no signal)", "partial"),
    ("R16", "Semi-finished shelf life", "hard", 2, None, "L8",
     "semi_finished_ageing", "open"),
    ("R17", "GT buffer floor tau >= tau_min", "hard", 5, None, "L7",
     "L0 tau", "open"),
    ("R18", "Frozen horizon respect", "hard", 1, None, "L13", "-", "open"),
    ("B12", "Lot floors + min demand", "hard", 3, 2000, "L4.5",
     "config.min_lot_units / min_demand_units", "enforced"),
    ("B16", "TT/TL machine dedication", "hard", 3, None, "L2/L6",
     "tt_tl + demand split", "open"),
]

COSTS = [
    (1, "late_demand", 2000, "per tyre-day", "R1/R12"),
    (2, "gt_age_breach", None, "HARD - no price", "R5"),
    (3, "split_cure_campaign", 300, "per split", "R9"),
    (3, "lot_below_min", 2000, "per lot", "R9/B12"),
    (3, "mould_change", None, "from press_mould_change, per event", "R3"),
    (4, "building_capacity", None, "HARD gate - no price", "R10"),
    (5, "press_starvation", None, "lambda x idle_h x margin", "R17"),
    (5, "gt_inventory", 50, "per |tau - tau*| hour", "R17"),
    (5, "overproduce_to_min_lot", 50, "per surplus tyre-day", "B12"),
    (5, "residual_deferred", 800, "per day late", "B12"),
    (6, "cure_changeover", None, "learned matrix", "R6/R7"),
    (7, "build_changeover", None, "learned x capacity factor - BELOW tier 6", "R7"),
    (8, "dedication_pcr", 10000, "near-hard", "R2"),
    (8, "dedication_tbr", 200, "soft", "R2"),
    (9, "sister_grouping_pcr", 500, "scaled to 5.02x measured lift", "R6"),
    (9, "sister_grouping_tbr", 200, "scaled to 2.48x measured lift", "R6"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()

    rep, _ctx = validate(a.month)
    sev_order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    rep = rep.with_columns(
        pl.col("severity").replace_strict(sev_order).alias("_o")
    ).sort(["_o", "check"]).drop("_o")

    print("=" * 92)
    print(f"L1  VALIDATION & CONSTRAINT COMPILATION   --   {a.month}")
    print("=" * 92)
    n_err = rep.filter(pl.col("severity") == "ERROR").height
    n_warn = rep.filter(pl.col("severity") == "WARN").height
    for r in rep.iter_rows(named=True):
        mark = {"ERROR": "[ERROR]", "WARN": "[WARN ]", "INFO": "[info ]"}[r["severity"]]
        rl = f" {r['rule']:<4}" if r["rule"] else "     "
        print(f"  {mark}{rl} {r['check']:<28} {r['detail']}")

    rt = pl.DataFrame(
        [{"rule_id": a_, "text": b, "class": c, "tier": d, "weight": e,
          "layer": f, "data_source": g, "status": h}
         for a_, b, c, d, e, f, g, h in RULES])
    rt.write_parquet(OUT / f"rule_table_{a.month}.parquet")
    ct = pl.DataFrame([{"tier": t, "item": i, "cost": c, "unit": u, "rule": r}
                       for t, i, c, u, r in COSTS])
    ct.write_parquet(OUT / "cost_table.parquet")
    rep.write_parquet(OUT / f"l1_findings_{a.month}.parquet")

    print()
    print(f"  rule table : {rt.height} rules  -> rule_table_{a.month}.parquet")
    print(f"    {'hard':<12}{rt.filter(pl.col('class')=='hard').height}")
    print(f"    {'soft/priced':<12}"
          f"{rt.filter(pl.col('class').str.starts_with('soft')).height}")
    print(f"    {'enforced':<12}{rt.filter(pl.col('status')=='enforced').height}"
          f"   partial {rt.filter(pl.col('status').str.starts_with('partial')).height}"
          f"   open {rt.filter(pl.col('status')=='open').height}")
    print(f"  cost table : {ct.height} entries across "
          f"{ct['tier'].n_unique()} tiers -> cost_table.parquet")
    print()
    print(f"  {'ERRORS':<8}{n_err}    {'WARN':<6}{n_warn}")
    print(f"  >>> {'BLOCKED -- fix errors before L2' if n_err else 'PASS -- proceed to L2'}")


if __name__ == "__main__":
    main()
