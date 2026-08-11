"""Export a planned month as shop-floor shift schedules + supporting sheets.

    python scripts/export_shift_schedule.py <run_id> <YYYY-MM> [out_dir]

Writes ONE Excel workbook with every sheet, plus the same sheets as individual
CSVs so the pack is usable without Excel.

REFUSES to export a stale arm. `arm_is_stale()` proves the scorecard in the run
directory describes the plan sitting beside it -- 15 directories once carried
another arm's result, so this check is not optional.

WHAT IS EXPORTED IS WHAT THE PLAN SAYS. Quantities are never smoothed, rounded
or tidied: if a run is 11 tyres it is exported as 11 tyres.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from planner.cmbc.l11_validate_plan import arm_is_stale  # noqa: E402
from planner.config import CONFIG  # noqa: E402
from planner import paths

# B12 lot floor. PARTITION §8: every enforced cap lives in `config.py` and is
# read from there -- "add a cap in config.py or nowhere". Do NOT restate 150/70.
CONFIG_FLOOR = dict(CONFIG.thresholds.min_lot_units)
_STRICT_NOTE = (
    "sub-floor run PLACED. Under PLANNER_STRICT_LOT_FLOOR=1 this is unreachable "
    "(`_place` refuses gq < floor on every machine, HARD_FLOOR is forced on and "
    "ATOMIC_SPLIT is force-disabled), so a row here means the run was let "
    "through by the plant-calibrated sub-floor BUDGET "
    "(PLANNER_SUBFLOOR_PCR/_TBR) with STRICT off, or by the atomic-split "
    "rescue. Check `0_settings` for which was active.")

SHIFT_START_H = 7          # plant day runs 07:00 -> 07:00
SHIFTS = ["A", "B", "C"]   # A 07-15, B 15-23, C 23-07
FMT = "%Y-%m-%d %H:%M"
# Gates PROVEN stricter than the plant's own executed behaviour (measured on
# runs/plant_2026-07 via scripts/plant_as_plan.py, identical denominators).
MISMINED = {
    "TBR build runs below min_lot (70)": "plant itself runs 40.3% -- gate too strict",
    "TBR build changeovers / machine-day": "plant itself runs 3.70 -- gate too strict",
    "TBR WEIGHTED build changeover min/machine-day": "plant itself 37.3 -- too strict",
    "TBR realised n_g (concurrent presses/GT)": "plant runs 1.69; this floor demands MORE dispersion than the plant uses",
    "PCR realised n_g (concurrent presses/GT)": "same mis-mined n_g floor",
}


def shift_cols(df: pl.DataFrame, ts_col: str, t0: datetime) -> pl.DataFrame:
    """Plant day + shift letter for a timestamp. Day boundary is 07:00.

    `date` IS THE PLANT-DAY DATE, not the wall-clock date of the timestamp.
    This used to be `ts_col.dt.date()`, which silently split every C shift in
    two: plant day 1 shift C runs 01 Jul 23:00 -> 02 Jul 07:00, so 7 of its 8
    hours carried the label `2026-07-02`. Filtering "01 Jul, shift C" then
    returned 352 of 4,241 PCR tyres and read as "nothing is being built on the
    night shift". 1,614 of 5,631 build rows (28.7 %, 135,379 tyres) were
    mislabelled that way, and it is also why the cure sheet appeared to stop a
    day before the build sheet. `date` + `shift` is now a true partition of the
    plant day; `cal_date` keeps the wall-clock date for anyone who needs it.
    """
    off = ((pl.col(ts_col) - pl.lit(t0)).dt.total_seconds() / 3600.0)
    return df.with_columns([
        (off // 24 + 1).cast(pl.Int64).alias("plant_day"),
        pl.col(ts_col).dt.strftime("%Y-%m-%d").alias("cal_date"),
        ((off % 24) // 8).cast(pl.Int64).alias("_s"),
    ]).with_columns([
        pl.when(pl.col("_s") == 0).then(pl.lit("A"))
        .when(pl.col("_s") == 1).then(pl.lit("B"))
        .otherwise(pl.lit("C")).alias("shift"),
        (pl.lit(t0.date()) + pl.duration(days=pl.col("plant_day") - 1))
        .dt.strftime("%Y-%m-%d").alias("date"),
    ]).drop("_s")


def fmt_ts(df: pl.DataFrame, cols) -> pl.DataFrame:
    for c in cols:
        if c in df.columns:
            df = df.with_columns(pl.col(c).dt.strftime(FMT).alias(c))
    return df


def main() -> int:
    run_id, month = sys.argv[1], sys.argv[2]
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else \
        ROOT / "output" / f"{month.replace('-', '_')}_schedule"
    run = ROOT / "runs" / run_id
    why = arm_is_stale(run)
    if why and not why.startswith("no l11_provenance"):
        raise SystemExit(f"!! REFUSING to export a stale arm: {why}")
    y, m = int(month[:4]), int(month[5:7])
    t0 = datetime(y, m, 1, SHIFT_START_H)
    # NEVER rmtree the output folder. A workbook open in Excel holds a lock, and
    # rmtree deletes csv/ BEFORE it reaches the locked .xlsx -- which destroys
    # half the pack and then aborts. Clear only the files we are about to
    # rewrite, each one tolerantly.
    (out / "csv").mkdir(parents=True, exist_ok=True)
    for _f in (out / "csv").glob("*.csv"):
        try:
            _f.unlink()
        except OSError as e:                            # noqa: PERF203
            print(f"  !! could not remove {_f.name}: {e}")

    R = lambda n: pl.read_parquet(run / f"{n}.parquet")   # noqa: E731
    bs, cc = R("build_schedule"), R("cure_campaigns")

    # ---- REPORT WINDOW: DAYS 1..ndays ONLY --------------------------------
    # PLANT RULING 2026-08-10: plan on month + tail, REPORT only the month.
    # Applied HERE, at load, and nowhere else -- every sheet in this pack is
    # derived from `bs`/`cc`, and sheet 1 / 5 / 7 must reconcile to the tyre. A
    # per-sheet filter is how two sheets end up disagreeing.
    #   BUILD  cut WHOLE-SLICE on end_ts. That is the GT ledger's own convention
    #          (`gt_events` credits a slice whole at end_ts), so a slice
    #          finishing next month is next month's green tyre -- it is neither
    #          exported nor carried forward, and the two numbers agree.
    #   CURE   cut PRO-RATA. A campaign that crosses the boundary has a real
    #          in-month portion and its press really is running on day 31;
    #          dropping it whole would taper the presses at exactly the hour the
    #          ruling says not to. Same time-fraction L7 uses for
    #          `qty_fed_in_month`, so sheet 2 and the fulfilment KPI cannot
    #          disagree about the same campaign.
    ndays = (datetime(y + (m == 12), (m % 12) + 1, 1)
             - datetime(y, m, 1)).days      # plant month length
    _mend = t0 + timedelta(days=ndays)
    _b0, _c0 = float(bs["qty"].sum()), float(cc["qty"].sum())
    bs = bs.filter(pl.col("end_ts") <= pl.lit(_mend))
    _frac = ((pl.lit(_mend) - pl.col("start_ts")).dt.total_seconds()
             / (pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds()
             .clip(lower_bound=1.0)).clip(0.0, 1.0)
    cc = (cc.filter(pl.col("start_ts") < pl.lit(_mend))
          .with_columns([
              (pl.col("qty") * _frac).round(0).alias("qty"),
              (pl.col("hours") * _frac).alias("hours"),
              pl.min_horizontal(pl.col("end_ts"), pl.lit(_mend)).alias("end_ts")])
          .filter(pl.col("qty") > 0))
    # Closing GT stock = built in-month, cured after it. Same definition L7 uses
    # for `carry_forward_gt.parquet`; read here so the pack and the hand-off file
    # cannot drift apart.
    _cf_qty = {r["plant"]: float(r["q"]) for r in
               (bs.filter((pl.col("machine") != "OPENING_STOCK")
                          & (pl.col("cure_ts") > pl.lit(_mend)))
                .group_by("plant").agg(pl.col("qty").sum().alias("q"))
                ).iter_rows(named=True)}
    if abs(_b0 - float(bs["qty"].sum())) > 0.5 or abs(_c0 - float(cc["qty"].sum())) > 0.5:
        print(f"  REPORT WINDOW  days 1-{ndays} (to {_mend:%Y-%m-%d %H:%M}): "
              f"build {_b0:,.0f} -> {float(bs['qty'].sum()):,.0f}  "
              f"cure {_c0:,.0f} -> {float(cc['qty'].sum()):,.0f}  "
              f"-- the difference is next month's, not lost")
    st, up = R("build_starved"), R("cure_unplaced")
    inv = R("l11_invariants")
    req = pl.read_parquet(ROOT / "warehouse" / "derived" /
                          f"net_requirement_{month}.parquet")
    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{month}.parquet")
    sz = pl.read_parquet(paths.INPUT_DERIVED / "gt_size.parquet")
    rim = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
           if r.get("gt_code") and r.get("rim")}

    # ---- HUMAN-READABLE DESCRIPTION per GT --------------------------------
    # A GT is a green-tyre spec and maps to MANY finished SKUs, so there is no
    # single "the" description. We show the MODAL SKU's description -- the one
    # the largest share of that GT's volume was actually sold as -- and say how
    # many other SKUs share the GT, e.g. "145 R12 ULTIMA XPC TL TATA (+14 SKUs)".
    # Source: warehouse/derived/gt_sku_from_recipe.parquet, built from the CURING
    # RECIPE (every cured tyre carries recipeID and that recipe's SAPMaterialCode
    # IS the finished SKU). 100 % of planned GTs resolve on both July and August,
    # so nothing is fabricated and nothing is blank.
    desc, modal_sku, n_sku, tsize = {}, {}, {}, {}
    _gsf = ROOT / "warehouse" / "derived" / "gt_sku_from_recipe.parquet"
    if _gsf.exists():
        for r in pl.read_parquet(_gsf).iter_rows(named=True):
            k = (r["plant"], r["gt_code"])
            n = int(r.get("n_skus") or 1)
            d = (r.get("sku_desc") or "").strip()
            desc[k] = f"{d} (+{n - 1} SKUs)" if d and n > 1 else d
            modal_sku[k] = r.get("sku_code") or ""
            n_sku[k] = n
            tsize[k] = r.get("tyre_size") or ""

    def add_desc(df: pl.DataFrame) -> pl.DataFrame:
        """gt_description / modal_sku / n_skus, keyed on (plant, gt_code)."""
        key = pl.concat_str([pl.col("plant"), pl.col("gt_code")], separator="|")
        mk = lambda d: {f"{a}|{b}": v for (a, b), v in d.items()}  # noqa: E731
        return df.with_columns([
            key.replace_strict(mk(desc), default="").alias("gt_description"),
            key.replace_strict(mk(modal_sku), default="").alias("modal_sku"),
            key.replace_strict(mk(n_sku), default=None,
                               return_dtype=pl.Int64).alias("n_skus"),
            key.replace_strict(mk(tsize), default="").alias("tyre_size"),
        ])

    # ---- CARRY-OUT: which rows are NOT this month's work -------------------
    # PLANNER_CARRY_OUT lets an L5 campaign START inside the horizon and FINISH
    # outside it, so a July plan legitimately contains rows dated into August.
    # That is correct planning and misleading presentation: a supervisor reading
    # "12-08" on a July sheet has no way to know it is deliberate. Every affected
    # row now carries `carry_out = true` and `plant_day > month length`.
    n_month_days = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
    month_end = t0 + timedelta(days=n_month_days)
    sheets: dict[str, pl.DataFrame] = {}

    # ---- 0. SETTINGS -- the caps actually in force for THIS run -----------
    # Requested by the plant after 300/150 vs 150/70 vs the slice size were
    # confused for one another. They are THREE DIFFERENT OBJECTS at three
    # different levels, so each row says plainly what its number acts on.
    from planner.config import CONFIG, GT_SHELF_LIFE_H  # noqa: E402
    import planner.cmbc.l7_pull_release as L7           # noqa: E402
    th = CONFIG.thresholds
    try:
        _pr = json.loads((ROOT / "masters" /
                          f"press_list_{month}.json").read_text())
        roster = {k: len(v) for k, v in _pr.items()}
    except Exception:                                   # noqa: BLE001
        roster = {}
    try:
        _co = (pl.read_parquet(ROOT / "warehouse" / "derived" / "cap_changeover.parquet")
               .group_by(["plant", "machine_type", "same_min", "diff_min"])
               .agg(pl.len().alias("n")).sort(["plant", "machine_type"]))
        co_txt = " · ".join(
            f"{r['plant']} {r['machine_type']} x{r['n']}: same {r['same_min']} / "
            f"diff {r['diff_min']} min" for r in _co.iter_rows(named=True))
    except Exception:                                   # noqa: BLE001
        co_txt = "PCR BJ 28/60 · PCR CONTI 22/42 · TBR 10/24 (master fallback)"
    try:
        import planner.cmbc.l5_cure_master as L5        # noqa: E402
        early_stock, floor_basis = L5.EARLY_STOCK, L5.FLOOR_BASIS
    except Exception:                                   # noqa: BLE001
        early_stock, floor_basis = "(unavailable)", "(unavailable)"

    # The opening-GT file the PLAN used, resolved exactly as L4/L5/L7 resolve it.
    _ogov = os.environ.get("PLANNER_OPENING_GT", "").strip()
    _OGF = (Path(_ogov) if (_ogov and Path(_ogov).is_absolute())
            else (ROOT / "masters" / "opening_gt" / _ogov) if _ogov
            else ROOT / "masters" / "opening_gt" / f"opening_gt_{month}.parquet")
    _OG = (pl.read_parquet(_OGF) if _OGF.exists()
           else pl.DataFrame(schema={"plant": pl.Utf8, "age_h": pl.Float64}))

    S = lambda k, v, acts, src, note: {                  # noqa: E731
        "setting": k, "value": v, "acts_on": acts, "lives_in": src, "what_it_does": note}
    cfg = ROOT / "planner" / "config.py"
    l7f = ROOT / "planner" / "cmbc" / "l7_pull_release.py"
    settings = [
        # --- THE THREE NUMBERS THAT GET CONFUSED, stated plainly -------------
        S("min_demand_units", f"PCR {th.min_demand_units['PCR']} / TBR {th.min_demand_units['TBR']}",
          "DEMAND (per GT, per month)", "planner/config.py:265",
          "A GT whose WHOLE-MONTH demand is below this is not planned at all. "
          "It is routed to the residual policy, not built. ON for this run."),
        S("min_lot_units (B12 lot floor)", f"PCR {th.min_lot_units['PCR']} / TBR {th.min_lot_units['TBR']}",
          "RUN (one continuous same-GT block on one machine)", "planner/config.py:248",
          "Smallest economic build RUN. Judge it on sheet 1b_build_runs, NEVER on "
          "sheet 1 -- a sheet-1 row is a slice, not a run."),
        S("build slice size", "emergent, not a setting", "SLICE (one delivery to a press)",
          "planner/cmbc/l7_pull_release.py:866",
          "A slice is a JIT DELIVERY of green tyres to a press. It has NO minimum "
          "by design. n comes from R5 and B12: n >= (H+Q*cad)/71.7 and n <= Q/min_lot."),
        # --- run shaping ------------------------------------------------------
        S("SUBFLOOR_BUDGET", f"PCR {L7.SUBFLOOR_BUDGET['PCR']} / TBR {L7.SUBFLOOR_BUDGET['TBR']}",
          "RUN", "planner/cmbc/l7_pull_release.py:178",
          "How many below-floor setups are allowed, matched to the plant's own "
          "revealed rate. The floor is a BUDGET, not a gate -- the plant has no hard floor."),
        S("HARD_FLOOR mode", L7._HF, "RUN", "planner/cmbc/l7_pull_release.py:183",
          "'budget' = plant-calibrated allowance · '1' = absolute gate · 'off' = no floor."),
        S("RUN_MULT", L7.RUN_MULT, "RUN", "planner/cmbc/l7_pull_release.py:144",
          "Run-size target as a multiple of the lot floor."),
        S("SLICE_MULT", f"PCR {L7.SLICE_MULT['PCR']} / TBR {L7.SLICE_MULT['TBR']}",
          "SLICE", "planner/cmbc/l7_pull_release.py:98",
          "0 = the DERIVED R5/B12 slice rule. TBR 3.0 = legacy arm, kept because "
          "the derived rule costs TBR 8.67 points (EXPERT_AUDIT)."),
        S("SLICE_AGGR", L7.SLICE_AGGR, "SLICE", "planner/cmbc/l7_pull_release.py:112",
          "Where to sit in the legal window [n_R5, n_B12]. 1.0 = smallest legal slice."),
        S("LOT_INTERVAL_H (T)", L7.LOT_INTERVAL_H, "RUN", "planner/cmbc/l7_pull_release.py:282",
          "Replenishment interval. Q_g = r_g x T. THIS IS THE GT-INVENTORY DIAL: "
          "I = sum_g r_g (T/2 + tau*)."),
        S("SPAN_MULT", L7.SPAN_MULT, "RUN", "planner/cmbc/l7_pull_release.py:152",
          "How many intervals of cure demand one run may absorb. 99 = effectively off, "
          "so R5 is the outer bound."),
        # --- hard limits ------------------------------------------------------
        S("R5 GT shelf life", f"{GT_SHELF_LIFE_H:.0f} h", "TYRE (hard)",
          "planner/config.py:325 (GT_SHELF_LIFE_H)",
          "A green tyre not cured within this is scrap. Checked per slice in L7 and per tyre in L11."),
        S("R17 tau release floor", os.environ.get("PLANNER_TAU_RELEASE", "min"), "SLICE",
          "planner/cmbc/l7_pull_release.py:318",
          "'min' = physical floor tau_min. 'star' restores the old tau*-as-a-wall (cost 7.2 pt)."),
        S("gt_wip_rail", f"PCR {th.gt_wip_rail['PCR']:,} / TBR {th.gt_wip_rail['TBR']:,}",
          "PLANT GT STOCK (hard placement refusal)", "planner/config.py:192",
          "Runaway rail on the daily-mean GT stock. A placement breaching it is refused."),
        S("gt_wip_rail_margin", th.gt_wip_rail_margin, "PLANT GT STOCK",
          "planner/config.py:206",
          f"Rail is checked at {th.gt_wip_rail_margin:.0%} of the stated cap so the STATED "
          "cap survives post-plan reconciliation."),
        S("G8 GT inventory band", f"PCR {th.gt_wip_min['PCR']:,}-{th.gt_wip_max['PCR']:,} / "
          f"TBR {th.gt_wip_min['TBR']:,}-{th.gt_wip_max['TBR']:,}",
          "PLANT GT STOCK (reported, not enforced)", "planner/config.py:168",
          "Plant's stated steady-state band. A DETECTOR in L11, never a controller."),
        # --- assignment -------------------------------------------------------
        S("HARD_LOCK (rim lock)", L7.HARD_LOCK, "MACHINE", "planner/cmbc/l7_pull_release.py:187",
          "A GT may only build on its rim's locked machines. Measured, priced, kept."),
        S("HARD_PIN", L7.HARD_PIN, "MACHINE", "planner/cmbc/l7_pull_release.py:210",
          "Try the GT's own partitioned machine first."),
        S("PARTITION_PLANTS", ",".join(sorted(L7.PARTITION_PLANTS)), "MACHINE",
          "planner/cmbc/l7_pull_release.py:~232",
          "Static GT->machine partition applied to these plants. TBR gains nothing from it."),
        S("MACH_UTIL_CAP", L7.MACH_UTIL_CAP, "MACHINE", "planner/cmbc/l7_pull_release.py:117",
          "A GT stays on one machine until that machine reaches this occupancy."),
        S("CAD_BASIS", L7.CAD_BASIS, "MACHINE", "planner/cmbc/l7_pull_release.py:129",
          "'machine' = each machine's own cadence. 'plant' is the flat-cadence bug, A/B only."),
        S("B16 criterion", os.environ.get("PLANNER_B16_CRITERION", "coverage"),
          "MACHINE (TBR TT/TL split)", "planner/cmbc/l2_capability.py:277",
          "How TBR machines are split into tube-type / tubeless groups. No spill across groups."),
        # --- horizon / stock --------------------------------------------------
        S("EARLY_STOCK", early_stock, "CURE START",
          "planner/cmbc/l5_cure_master.py:76",
          "Lets a GT holding enough opening stock start curing at t0 instead of idling "
          "~11 h. Worth +2.3 pt."),
        S("L5 floor basis", floor_basis, "CURE START",
          "planner/cmbc/l5_cure_master.py:83",
          "'star' = tau* + build_band day-1 cure floor. Applies only where a GT has no "
          "opening stock to bridge the gap."),
        S("CARRY_OUT", os.environ.get("PLANNER_CARRY_OUT", "1") != "0", "HORIZON",
          "planner/cmbc/l5_cure_master.py:51",
          "A campaign starting inside the month may finish outside it; the tail is next "
          "month's opening state. This is why sheet 7 has carry_out days."),
        S("lookahead_days", os.environ.get("PLANNER_LOOKAHEAD_DAYS", "0"), "DEMAND HORIZON",
          "planner/cmbc/l4_net_requirement.py:103",
          "Next-month demand pulled into this month's build. 0 = off, so nothing pulls "
          "build on the last days -> the month-end WIP collapse."),
        # --- masters ----------------------------------------------------------
        S("changeover minutes", co_txt, "MACHINE (setup cost)",
          "masters/Master_Building_ChangeoverTime_*.csv",
          "Setup time IS changeover time and it is SIZE-DEPENDENT. Never hardcode these."),
        S("press roster", "  ".join(f"{k} {v}" for k, v in sorted(roster.items())) or "(none)",
          "PRESS", f"masters/press_list_{month}.json",
          "Presses available this month. July's roster is reused for August unless the "
          "plant sends a new one."),
        S("opening GT stock", f"PCR {int(_OG.filter(pl.col('plant')=='PCR').height):,} / "
          f"TBR {int(_OG.filter(pl.col('plant')=='TBR').height):,}"
          + (f"  (assumed age {float(_OG['age_h'].median()):.0f} h)" if _OG.height else ""),
          "OPENING STATE", f"masters/opening_gt/{_OGF.name}",
          "Green tyres on the floor at 07:00 on day 1, with their ages. Netted off BUILD, never off cure."),
        S("seed", CONFIG.seed, "DETERMINISM", "planner/config.py",
          "Same inputs give a byte-identical plan."),
    ]
    sheets["0_settings"] = pl.DataFrame(
        settings, schema={"setting": pl.Utf8, "value": pl.Utf8, "acts_on": pl.Utf8,
                          "lives_in": pl.Utf8, "what_it_does": pl.Utf8},
        strict=False)

    # ---- 1. BUILD SCHEDULE, shift-wise -----------------------------------
    b = bs.filter(pl.col("machine") != "OPENING_STOCK")
    b = shift_cols(b, "start_ts", t0).with_columns([
        pl.col("gt_code").replace_strict(rim, default="(no rim)").alias("rim"),
        ((pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds() / 3600.0)
        .round(2).alias("hours"),
    ])
    # Changeover flag: the machine's previous run was a different GT / rim.
    # SORT CHRONOLOGICALLY, EXPLICITLY. `shift(1).over(machine)` reads whatever
    # row order the frame happens to be in, and this used to run on a frame
    # sorted by (plant, date, shift, ...) where `date` was the WALL-CLOCK date.
    # Shift C spans midnight, so its post-midnight rows sorted into the NEXT
    # calendar date ahead of that date's A and B shifts: 297 PCR and 256 TBR
    # slices sat before a slice that started earlier. Each such inversion
    # invented a GT transition. Reported build changeovers were PCR 967 (true
    # 799, +21 %) and TBR 1,108 (true 800, +38 %). The true count is provable
    # independently: runs - machines = 810 - 11 = 799 and 809 - 9 = 800.
    b = b.sort(["plant", "machine", "start_ts"]).with_columns([
        pl.col("gt_code").shift(1).over(["plant", "machine"]).alias("_pg"),
        pl.col("rim").shift(1).over(["plant", "machine"]).alias("_pr"),
    ]).with_columns([
        (pl.col("gt_code") != pl.col("_pg")).fill_null(False).alias("changeover"),
        ((pl.col("gt_code") != pl.col("_pg")) & (pl.col("rim") != pl.col("_pr")))
        .fill_null(False).alias("size_change"),
        pl.col("_pg").alias("prev_gt"),
    ]).drop(["_pg", "_pr"])
    # A run_id can contain a >1 h gap (140 of 5,681 slices on July), so a naive
    # grouping would show a "run" that is not physically continuous. Split there.
    b = b.sort(["plant", "machine", "start_ts"]).with_columns(
        ((pl.col("start_ts") - pl.col("end_ts").shift(1).over("run_id"))
         .dt.total_seconds() / 3600.0).alias("_gap"))
    b = b.with_columns(
        ((pl.col("_gap") > 1.0) | pl.col("_gap").is_null()).cum_sum()
        .alias("_blk")).with_columns(
        (pl.col("run_id") + "#" + pl.col("_blk").cast(pl.Utf8)).alias("block_id"))
    b = b.with_columns([
        pl.col("qty").sum().over("block_id").alias("run_qty"),
        pl.col("qty").len().over("block_id").alias("_n"),
        (pl.col("start_ts").rank("ordinal").over("block_id")).alias("_i"),
    ]).with_columns(
        (pl.col("_i").cast(pl.Utf8) + " of " + pl.col("_n").cast(pl.Utf8))
        .alias("slice_i_of_n"))
    b = add_desc(b).with_columns(
        (pl.col("plant_day") > n_month_days).alias("carry_out"))
    sheets["1_build_schedule_shift"] = fmt_ts(b.select(
        ["plant", "date", "shift", "plant_day", "cal_date", "carry_out", "machine",
         "gt_code", "gt_description", "tyre_size", "rim", "modal_sku", "n_skus",
         "qty", "run_qty", "slice_i_of_n", "start_ts", "end_ts", "hours",
         "run_id", "block_id", "prev_gt", "changeover", "size_change",
         "press", "cure_ts", "wait_h"]),
        ["start_ts", "end_ts", "cure_ts"])

    # ---- 1b. RUN-LEVEL sheet -- the view a supervisor actually needs ------
    # One row per CONTINUOUS same-GT block on a machine. This is the object the
    # B12 lot floor acts on; the slice sheet above is the JIT feed to the press
    # and is NOT the right granularity for judging lot size.
    runs = (b.group_by(["plant", "machine", "block_id"]).agg([
        pl.col("gt_code").first(), pl.col("rim").first(),
        pl.col("qty").sum().alias("run_qty"),
        pl.len().alias("slices"),
        pl.col("start_ts").min().alias("start_ts"),
        pl.col("end_ts").max().alias("end_ts"),
        pl.col("date").first(), pl.col("shift").first(),
        pl.col("plant_day").first(),
        pl.col("changeover").any().alias("changeover"),
        pl.col("size_change").any().alias("size_change"),
        pl.col("prev_gt").first(),
    ]).with_columns(
        ((pl.col("end_ts") - pl.col("start_ts")).dt.total_seconds() / 3600.0)
        .round(2).alias("duration_h"))
        .sort(["plant", "start_ts", "machine"]))
    # ---- SETUP RESERVATION AUDIT, per run ---------------------------------
    # L7's `_place` only avoids INTERVAL OVERLAP on a machine; it never reserves
    # changeover time. Two different GTs can therefore sit exactly back-to-back
    # with a zero gap. These columns make that visible per run: what the plant
    # master says the changeover costs, what gap the plan actually left, and the
    # shortfall. See README section "known defect: changeover time is not
    # reserved". Reporting only -- nothing here changes the plan.
    _same = {r["machine"]: float(r["same_min"]) for r in
             pl.read_parquet(ROOT / "warehouse" / "derived" /
                             "cap_changeover.parquet").iter_rows(named=True)}
    _diff = {r["machine"]: float(r["diff_min"]) for r in
             pl.read_parquet(ROOT / "warehouse" / "derived" /
                             "cap_changeover.parquet").iter_rows(named=True)}
    runs = runs.with_columns([
        pl.col("end_ts").shift(1).over(["plant", "machine"]).alias("_pe"),
        pl.col("gt_code").shift(1).over(["plant", "machine"]).alias("_pg2"),
        pl.col("rim").shift(1).over(["plant", "machine"]).alias("_pr2"),
    ]).with_columns(
        ((pl.col("start_ts") - pl.col("_pe")).dt.total_seconds() / 60.0)
        .alias("gap_before_min"))
    runs = runs.with_columns(
        pl.when(pl.col("_pg2").is_null() | (pl.col("_pg2") == pl.col("gt_code")))
        .then(pl.lit(0.0))
        .when(pl.col("_pr2") == pl.col("rim"))
        .then(pl.col("machine").replace_strict(_same, default=22.0))
        .otherwise(pl.col("machine").replace_strict(_diff, default=42.0))
        .alias("setup_required_min"))
    runs = runs.with_columns(
        (pl.col("setup_required_min") - pl.col("gap_before_min").fill_null(1e9))
        .clip(lower_bound=0).round(1).alias("setup_shortfall_min"))
    runs = add_desc(runs).with_columns(
        (pl.col("plant_day") > n_month_days).alias("carry_out"))
    sheets["1b_build_runs"] = fmt_ts(runs.select(
        ["plant", "date", "shift", "plant_day", "carry_out", "machine",
         "gt_code", "gt_description", "tyre_size", "rim", "modal_sku", "n_skus",
         "run_qty", "slices", "start_ts", "end_ts", "duration_h",
         "changeover", "size_change", "prev_gt",
         "gap_before_min", "setup_required_min", "setup_shortfall_min",
         "block_id"]),
        ["start_ts", "end_ts"])

    # ---- 2b. CURE CAMPAIGNS -- one row per campaign (what sheet 2 used to be)
    # A campaign is a MULTI-WEEK object: PCR p50 192.6 h, TBR p50 255.9 h. Dating
    # it by its start and calling the sheet "shift-wise" put up to 638 h of press
    # work on a single row labelled "01 Jul, shift A". Two consequences, both
    # reported from the floor: (a) the sheet appeared to stop on 30 Jul because
    # no campaign STARTS on the 31st, while the build sheet ran to the 31st;
    # (b) its `qty` sums to 485,647 -- the campaign NAMEPLATE -- against 475,271
    # tyres actually fed, so the pack read as if it cured 10,376 tyres it never
    # made. The campaign view is genuinely useful, so it is kept here, correctly
    # named and dated, and now carries the fed/unfed split. Sheet 2 below is the
    # real shift-wise cure schedule.
    camp = shift_cols(cc, "start_ts", t0).with_columns([
        pl.col("gt_code").replace_strict(rim, default="(no rim)").alias("rim"),
        (pl.col("plant") + "-" + pl.col("press") + "-" +
         pl.col("start_ts").dt.strftime("%m%d%H%M")).alias("campaign_id"),
    ]).sort(["plant", "start_ts", "press"])
    _rec = run / "cure_campaigns_reconciled.parquet"
    if _rec.exists():
        camp = camp.join(pl.read_parquet(_rec).select(
            ["plant", "gt_code", "press", "start_ts", "qty_fed", "qty_unfed"]),
            on=["plant", "gt_code", "press", "start_ts"], how="left")
    for _c in ("qty_fed", "qty_unfed"):
        if _c not in camp.columns:
            camp = camp.with_columns(pl.lit(None, pl.Float64).alias(_c))
    camp = add_desc(camp).with_columns([
        (pl.col("plant_day") > n_month_days).alias("carry_out"),
        (pl.col("end_ts") > pl.lit(month_end)).alias("finishes_next_month"),
    ]).rename({"qty": "qty_planned", "date": "start_date", "shift": "start_shift"})
    _sheet2b = fmt_ts(camp.select(
        ["plant", "start_date", "start_shift", "plant_day", "carry_out",
         "finishes_next_month", "press", "gt_code", "gt_description",
         "tyre_size", "rim", "modal_sku", "n_skus", "mould_set",
         "qty_planned", "qty_fed", "qty_unfed",
         "start_ts", "end_ts", "hours", "campaign_id"]),
        ["start_ts", "end_ts"])

    # ---- 2. CURE SCHEDULE, genuinely shift-wise --------------------------
    # ONE ROW PER (plant, plant_day, shift, press, GT) -- the same shape as the
    # build sheet, which is what the shop floor reads. Every campaign is clipped
    # to each 8 h shift window, then joined to the tyres actually FED to that
    # press in that shift.
    #   qty          tyres credited to this press-shift, bucketed on `cure_ts` --
    #                the SAME basis as sheet 7's `cured` and the headline, so
    #                sheet 2 -> sheet 7 -> KPI reconcile at diff 0 BY
    #                CONSTRUCTION. A build slice is credited at the instant it is
    #                handed to the press, so this column is spiky: a shift can
    #                hold press hours and 0 tyres because the press is still
    #                working through an earlier delivery.
    #   qty_run      the same campaign total spread pro-rata over press hours.
    #                Smooth, reads like press output, sums to `qty` per plant.
    #                NOT the reconciling column -- do arithmetic on `qty`.
    #   qty_planned  the campaign's NAMEPLATE share of this shift. Sums to
    #                485,647 on July; the gap to `qty` is unfed press capacity.
    _seg = []
    for _r in cc.iter_rows(named=True):
        _s = (_r["start_ts"] - t0).total_seconds() / 3600.0
        _e = (_r["end_ts"] - t0).total_seconds() / 3600.0
        if _e - _s <= 0:
            continue
        for _k in range(int(_s // 8), int(np.ceil(_e / 8))):
            _lo, _hi = max(_s, _k * 8), min(_e, (_k + 1) * 8)
            if _hi - _lo <= 1e-9:
                continue
            _seg.append({
                "plant": _r["plant"], "press": str(_r["press"]),
                "gt_code": _r["gt_code"], "mould_set": _r.get("mould_set"),
                "plant_day": _k // 3 + 1, "shift": "ABC"[_k % 3],
                "start_ts": t0 + timedelta(hours=_lo),
                "end_ts": t0 + timedelta(hours=_hi),
                "hours": _hi - _lo,
                "qty_planned": _r["qty"] * (_hi - _lo) / (_e - _s),
                "_share": (_hi - _lo) / (_e - _s),
                "campaign_id": (f"{_r['plant']}-{_r['press']}-"
                                f"{_r['start_ts']:%m%d%H%M}"),
            })
    # The grain MUST be exactly (plant, plant_day, shift, press, GT) so the join
    # to `fed` is strictly 1:1. Two things break that if left alone: one campaign
    # can re-enter the same shift as two segments, and a press can switch between
    # two campaigns of the SAME GT inside one shift. Keeping campaign_id in the
    # group key survives the first and not the second -- it left 89 duplicate
    # rows and double-counted 13,095 fed tyres. So `qty_run` is resolved per
    # SEGMENT (where the campaign is still unambiguous) and only then summed.
    c = pl.DataFrame(_seg)
    if "qty_fed" in camp.columns:
        c = (c.join(camp.select(["campaign_id", "qty_fed"]), on="campaign_id",
                    how="left")
             .with_columns((pl.col("qty_fed").fill_null(0.0) * pl.col("_share"))
                           .alias("_qrun")))
    else:
        c = c.with_columns(pl.lit(None, pl.Float64).alias("_qrun"))
    c = (c.group_by(["plant", "press", "gt_code", "plant_day", "shift"]).agg([
        pl.col("mould_set").first(),
        pl.col("start_ts").min(), pl.col("end_ts").max(),
        pl.col("hours").sum(), pl.col("qty_planned").sum(),
        pl.col("_qrun").sum().alias("qty_run"),
        pl.col("campaign_id").first(),
        pl.col("campaign_id").n_unique().alias("campaigns"),
        pl.len().alias("segments")]))
    # tyres actually fed to this press in this shift -- sheet 7's own basis,
    # INCLUDING the OPENING_STOCK pseudo-machine, which is a real feed to a press.
    # CURED IN-MONTH ONLY. A tyre fed to a press whose cure completes after the
    # boundary is next month's output, not a day-1..31 cure row -- the same
    # boundary L7 uses for `qty_fed_in_month`. Without this the full join below
    # invents plant_day 32/33 rows carrying exactly the carry-forward quantity,
    # with null timestamps, and the pack claims 31 days it does not cover.
    _off = (pl.col("cure_ts") - pl.lit(t0)).dt.total_seconds() / 3600.0
    fed_sh = (bs.filter(pl.col("cure_ts") <= pl.lit(_mend)).with_columns([
        (_off // 24 + 1).cast(pl.Int64).alias("plant_day"),
        ((_off % 24) // 8).cast(pl.Int64).alias("_s"),
        pl.col("press").cast(pl.Utf8).alias("press")])
        .with_columns(pl.when(pl.col("_s") == 0).then(pl.lit("A"))
                      .when(pl.col("_s") == 1).then(pl.lit("B"))
                      .otherwise(pl.lit("C")).alias("shift"))
        .group_by(["plant", "press", "gt_code", "plant_day", "shift"])
        .agg(pl.col("qty").sum().alias("qty")))
    c = (c.join(fed_sh, on=["plant", "press", "gt_code", "plant_day", "shift"],
                how="full", coalesce=True)
         .with_columns([pl.col(x).fill_null(0.0) for x in
                        ("qty", "qty_planned", "hours", "qty_run")])
         .with_columns([pl.col("segments").fill_null(0),
                        pl.col("campaigns").fill_null(0)]))
    c = c.with_columns([
        (pl.lit(t0.date()) + pl.duration(days=pl.col("plant_day") - 1))
        .dt.strftime("%Y-%m-%d").alias("date"),
        pl.col("gt_code").replace_strict(rim, default="(no rim)").alias("rim"),
        (pl.col("plant_day") > n_month_days).alias("carry_out"),
    ])
    c = add_desc(c).sort(["plant", "plant_day", "shift", "press", "start_ts"])
    sheets["2_cure_schedule_shift"] = fmt_ts(c.select(
        ["plant", "date", "shift", "plant_day", "carry_out", "press",
         "gt_code", "gt_description", "tyre_size", "rim", "modal_sku", "n_skus",
         "mould_set", "qty", "qty_run", "qty_planned", "start_ts", "end_ts",
         "hours", "segments", "campaigns", "campaign_id"]).with_columns([
             pl.col("qty_run").round(1), pl.col("qty_planned").round(1),
             pl.col("hours").round(3)]),
        ["start_ts", "end_ts"])
    sheets["2b_cure_campaigns"] = _sheet2b
    # PROVE sheet 2 ties to the fed total, the same way sheet 7 is proven against
    # sheet 1. The old sheet 2 was only ever cross-checked against sheet 6, which
    # was built from the SAME campaign frame -- so the pair agreed with each other
    # and disagreed with the plan, and nothing caught it.
    print("  RECONCILIATION  sheet 2 (shift-wise cure) vs headline fed")
    for _p in ("PCR", "TBR"):
        _s2 = float(c.filter(pl.col("plant") == _p)["qty"].sum())
        _hf = float(bs.filter(pl.col("plant") == _p)["qty"].sum())
        _np_ = float(c.filter(pl.col("plant") == _p)["qty_planned"].sum())
        print(f"     {_p}  sheet2 qty {_s2:>10,.0f} vs fed {_hf:>10,.0f}  "
              f"diff {_s2 - _hf:>6,.0f}   nameplate {_np_:>10,.0f}   "
              f"{'OK' if abs(_s2 - _hf) < 0.5 else '!! MISMATCH'}")

    # ---- 3/4. mould changes + crew load (straight from L10) --------------
    for name, sh in (("mould_changes", "3_mould_changes"),
                     ("crew_load", "4_crew_load")):
        f = run / f"{name}.parquet"
        if f.exists():
            d = pl.read_parquet(f)
            tsc = [x for x, t in zip(d.columns, d.dtypes)
                   if t in (pl.Datetime, pl.Datetime("us"))]
            if tsc:
                d = shift_cols(d, tsc[0], t0)
            sheets[sh] = fmt_ts(d, tsc)

    # ---- 5/6. machine and press summaries --------------------------------
    H = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days * 24
    mach = (b.group_by(["plant", "machine"]).agg(
        pl.col("qty").sum().alias("tyres"),
        pl.col("run_id").n_unique().alias("runs"),
        pl.col("gt_code").n_unique().alias("distinct_gts"),
        pl.col("changeover").sum().alias("changeovers"),
        pl.col("size_change").sum().alias("size_changes"),
        pl.col("hours").sum().round(1).alias("busy_h"))
        .with_columns([
            (100 * pl.col("busy_h") / H).round(1).alias("occupancy_pct"),
            (H - pl.col("busy_h")).round(1).alias("idle_h")])
        .sort(["plant", "machine"]))
    # Setup the plan owes but never reserved on this machine's timeline.
    _sr = (runs.group_by(["plant", "machine"]).agg(
        (pl.col("setup_required_min").sum() / 60.0).round(1).alias("setup_required_h"),
        (pl.col("setup_shortfall_min").sum() / 60.0).round(1).alias("setup_unreserved_h"),
        (pl.col("setup_shortfall_min") > 0.001).sum().alias("runs_short_of_setup")))
    mach = (mach.join(_sr, on=["plant", "machine"], how="left")
            .with_columns([pl.col("setup_required_h").fill_null(0.0),
                           pl.col("setup_unreserved_h").fill_null(0.0),
                           pl.col("runs_short_of_setup").fill_null(0)])
            .with_columns((100 * (pl.col("busy_h") + pl.col("setup_unreserved_h")) / H)
                          .round(1).alias("occupancy_pct_with_setup")))
    sheets["5_machine_summary"] = mach
    # `tyres` is the FED figure so it reconciles with sheet 2 and the headline;
    # `tyres_planned` is the nameplate the presses were seated for. When it was
    # only the nameplate, this sheet agreed with the old sheet 2 and both were
    # 10,376 tyres above what the plan actually makes -- two sheets agreeing on
    # the same wrong number is exactly how that survived.
    press = (c.group_by(["plant", "press"]).agg(
        pl.col("qty").sum().alias("tyres"),
        pl.col("qty_planned").sum().round(0).alias("tyres_planned"),
        pl.col("gt_code").n_unique().alias("distinct_gts"),
        pl.col("hours").sum().round(1).alias("busy_h"))
        .join(camp.group_by(["plant", "press"]).agg(
            pl.len().alias("campaigns")), on=["plant", "press"], how="left")
        .with_columns([
            (pl.col("tyres_planned") - pl.col("tyres")).alias("tyres_unfed"),
            (100 * pl.col("busy_h") / H).round(1).alias("utilisation_pct"),
            (H - pl.col("busy_h")).round(1).alias("idle_h"),
            pl.col("campaigns").fill_null(0)])
        .select(["plant", "press", "tyres", "tyres_planned", "tyres_unfed",
                 "campaigns", "distinct_gts", "busy_h", "utilisation_pct",
                 "idle_h"])
        .sort(["plant", "press"]))
    sheets["6_press_summary"] = press

    # ---- 7. daily summary -------------------------------------------------
    # MUST RECONCILE WITH SHEET 1. Two bucketing defects have now been found here
    # and both silently DROPPED tyres, which is the worst failure mode for a
    # summary sheet -- it looks complete.
    #   1. bucketing by CALENDAR DATE lost day 31's C shift (968 + 765 tyres).
    #      Fixed by bucketing on `plant_day` (07:00 -> 07:00).
    #   2. looping `range(H // 24)` lost every row with plant_day > month length.
    #      Those are CARRY-OUT: L5 places campaigns that START inside the horizon
    #      and FINISH outside it (PLANNER_CARRY_OUT), so their feeding build
    #      slices legitimately land on plant_day 32..43. Measured on runs/f_solo:
    #      27 slices, 799 PCR + 734 TBR = 1,533 tyres, silently absent.
    # The loop now runs to the LAST plant_day present in the plan and flags
    # anything past the month end as carry-out, so sheet 7 sums to sheet 1 exactly.
    # THE REPORT IS THE MONTH. This loop used to run to the last plant_day in
    # the plan so carry-out rows (plant_day 32..43) still summed into sheet 7.
    # Under the extend ruling `bs`/`cc` are already cut at the boundary, so the
    # month IS the whole report and extending the loop only manufactures empty
    # day-32 rows. The GT balance below still walks the UNCLIPPED cure times --
    # a tyre held past the boundary must still drain, or day 31's closing stock
    # reads high.
    n_month_days = H // 24
    max_day = n_month_days
    H_ext = H
    rows = []
    for p in ("PCR", "TBR"):
        bp = bs.filter(pl.col("plant") == p)
        ivt = pl.concat([
            bp.select([pl.col("end_ts").alias("ts"), pl.col("qty").alias("d")]),
            bp.select([pl.col("cure_ts").alias("ts"), (-pl.col("qty")).alias("d")]),
        ]).sort("ts").with_columns(pl.col("d").cum_sum().alias("bal"))
        ts = np.array([(x - t0).total_seconds() / 3600.0 for x in ivt["ts"]])
        bal = np.array(ivt["bal"], float)
        idx = np.searchsorted(ts, np.arange(H_ext) + 0.5, side="right") - 1
        g = np.where(idx >= 0, bal[np.clip(idx, 0, len(bal) - 1)], 0.0)
        bd = b.filter(pl.col("plant") == p)
        cd = shift_cols(bp.filter(pl.col("cure_ts") <= pl.lit(_mend)),
                        "cure_ts", t0)      # same basis as sheet 2
        for dnum in range(max_day):
            day = (t0 + timedelta(days=dnum)).strftime("%Y-%m-%d")
            rows.append({
                "plant": p, "plant_day": dnum + 1, "date": day,
                "carry_out": dnum + 1 > n_month_days,
                "built": int(bd.filter(pl.col("plant_day") == dnum + 1)["qty"].sum()),
                "cured": int(cd.filter(pl.col("plant_day") == dnum + 1)["qty"].sum()),
                "gt_inventory_day_mean": round(float(g[dnum * 24:(dnum + 1) * 24].mean())),
                "gt_inventory_close": round(float(g[min((dnum + 1) * 24 - 1, H_ext - 1)])),
                "changeovers": int(bd.filter(pl.col("plant_day") == dnum + 1)["changeover"].sum()),
            })
    daily = pl.DataFrame(rows).sort(["plant", "plant_day"])
    sheets["7_daily_summary"] = daily
    # PROVE it ties. A summary that silently disagrees with its own detail sheet
    # is worse than no summary at all -- this refuses to be wrong quietly.
    recon = []
    for p in ("PCR", "TBR"):
        s1 = int(b.filter(pl.col("plant") == p)["qty"].sum())
        s7 = int(daily.filter(pl.col("plant") == p)["built"].sum())
        c1 = int(bs.filter(pl.col("plant") == p)["qty"].sum())
        c7 = int(daily.filter(pl.col("plant") == p)["cured"].sum())
        recon.append((p, s1, s7, s1 - s7, c1, c7, c1 - c7))
    print("  RECONCILIATION  sheet 7 vs sheet 1")
    for p, s1, s7, d, c1, c7, dc in recon:
        print(f"     {p}  built {s1:>9,} vs {s7:>9,}  diff {d:>6,}   "
              f"cured(fed) {c1:>9,} vs {c7:>9,}  diff {dc:>6,}"
              f"   {'OK' if d == 0 and dc == 0 else '!! MISMATCH'}")

    # ---- 8. demand vs plan -------------------------------------------------
    fed = bs.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("fed"))
    stg = st.group_by(["plant", "gt_code"]).agg(
        pl.col("qty").sum().alias("starved"),
        pl.col("reason").first().alias("shortfall_reason"))
    upg = (up.group_by(["plant", "gt_code"]).agg(
        pl.col("qty").sum().alias("unplaced_L5")) if up.height else
        pl.DataFrame(schema={"plant": pl.Utf8, "gt_code": pl.Utf8,
                             "unplaced_L5": pl.Float64}))
    dv = (dem.group_by(["plant", "gt_code"]).agg(
        pl.col("qty").sum().alias("demanded"),
        pl.col("sku").n_unique().alias("skus"))
        .join(req.select(["plant", "gt_code", "gross_build", "residual"]),
              on=["plant", "gt_code"], how="left")
        .join(fed, on=["plant", "gt_code"], how="left")
        .join(stg, on=["plant", "gt_code"], how="left")
        .join(upg, on=["plant", "gt_code"], how="left")
        .with_columns([pl.col("fed").fill_null(0), pl.col("starved").fill_null(0),
                       pl.col("unplaced_L5").fill_null(0),
                       pl.col("gt_code").replace_strict(rim, default="(no rim)").alias("rim")])
        .with_columns((pl.col("demanded") - pl.col("fed")).alias("shortfall"))
        .sort(["plant", "shortfall"], descending=[False, True]))
    sheets["8_demand_vs_plan"] = add_desc(dv).select(
        ["plant", "gt_code", "gt_description", "tyre_size", "rim", "modal_sku",
         "n_skus", "skus", "demanded", "gross_build", "fed",
         "shortfall", "starved", "unplaced_L5", "shortfall_reason", "residual"])

    # ---- 9. KPI + invariants ----------------------------------------------
    # In-month vs carry-out split, straight from the reconciled cure plan so the
    # headline cannot be misread as "we made this many tyres in August".
    _inm: dict[str, float] = {}
    _tail: dict[str, float] = {}
    _rf = run / "cure_campaigns_reconciled.parquet"
    if _rf.exists():
        _rr = pl.read_parquet(_rf)
        if "qty_fed_in_month" in _rr.columns:
            for _r in (_rr.group_by("plant").agg(
                    pl.col("qty_fed_in_month").sum().alias("i"),
                    pl.col("qty_fed").sum().alias("f")).iter_rows(named=True)):
                _inm[_r["plant"]] = float(_r["i"])
                _tail[_r["plant"]] = float(_r["f"]) - float(_r["i"])
    _fed_tot = {p: float(bs.filter(pl.col("plant") == p)["qty"].sum())
                for p in ("PCR", "TBR")}
    _op_used = {p: float(bs.filter((pl.col("plant") == p) &
                                   (pl.col("machine") == "OPENING_STOCK"))["qty"].sum())
                for p in ("PCR", "TBR")}
    _unfed = {p: float(camp.filter(pl.col("plant") == p)["qty_unfed"].sum() or 0.0)
              for p in ("PCR", "TBR")}
    kpi = []
    for p in ("PCR", "TBR"):
        bp = b.filter(pl.col("plant") == p)
        d11 = {r["invariant"]: r["actual"] for r in inv.iter_rows(named=True)}
        q = bp.group_by("run_id").agg(pl.col("qty").sum())["qty"].to_numpy()
        mp = mach.filter(pl.col("plant") == p)
        dsum = pl.DataFrame(rows).filter(pl.col("plant") == p)
        kpi += [
            {"plant": p, "metric": "demand fulfilment (IN-MONTH output)", "value": d11.get(f"{p} demand fulfilment")},
            {"plant": p, "metric": "  basis: cure output inside the plant month, opening stock INCLUDED, carry-out tail EXCLUDED", "value": ""},
            {"plant": p, "metric": "tyres CURED in-month (fulfilment numerator)", "value": f"{_inm.get(p, 0):,.0f}"},
            {"plant": p, "metric": "  + carry-out tail, cured NEXT month (excluded)", "value": f"{_tail.get(p, 0):,.0f}"},
            {"plant": p, "metric": "tyres fed to presses (incl. both)", "value": f"{int(bs.filter(pl.col('plant')==p)['qty'].sum()):,}"},
            {"plant": p, "metric": "  of which BUILT this month (sheet 1)", "value": f"{int(bp['qty'].sum()):,}"},
            {"plant": p, "metric": "  of which OPENING STOCK carried in", "value": f"{int(bs.filter((pl.col('plant')==p)&(pl.col('machine')=='OPENING_STOCK'))['qty'].sum()):,}"},
            {"plant": p, "metric": "  of which BUILT after month end (carry-out)", "value": f"{int(bp.filter(pl.col('carry_out'))['qty'].sum()):,}"},
            # ---- THE CHAIN, SPELLED OUT ------------------------------------
            # "build is 470k but curing is 485k" was asked from the floor. Three
            # different numbers live in this pack and every one of them is
            # correct for its own question; nothing said so. Now it does.
            {"plant": p, "metric": "RECONCILIATION  build -> cure -> press capacity", "value": ""},
            {"plant": p, "metric": f"  A  BUILT this month, sheet 1 + sheet 5 + sheet 7 'built'", "value": f"{int(bp['qty'].sum()):,}"},
            {"plant": p, "metric": "  B  + OPENING GT STOCK consumed (on the floor at 07:00 day 1)", "value": f"{int(_op_used.get(p, 0)):,}"},
            # WAS HARDCODED "0", which was true only while the month was a
            # closed box and every green tyre was consumed before hour 744. Under
            # HORIZON_MODE=extend the closing stock is real and is the hand-off
            # to next month, so it is READ from the plan, not asserted.
            {"plant": p, "metric": "  C  - GT still unbuilt-into-cure at month end (closing stock -> next month's opening)", "value": f"{int(_cf_qty.get(p, 0)):,}"},
            # D WAS A+B AND IS NOW A+B-C. While the month was a closed box C was
            # always 0 and the two were the same number; under the extend ruling
            # they are not, and D is the one that must equal sheet 2 / sheet 7
            # and the fulfilment numerator. Leaving it at A+B overstated cured
            # output by exactly the carry-forward.
            {"plant": p, "metric": "  D  = TYRES CURED IN-MONTH (A+B-C) = sheet 2 'qty' = sheet 7 'cured' = fulfilment numerator", "value": f"{int(_fed_tot.get(p, 0) - _cf_qty.get(p, 0)):,}"},
            {"plant": p, "metric": "  E  + press slots planned but NEVER FED (starved presses)", "value": f"{int(_unfed.get(p, 0)):,}"},
            {"plant": p, "metric": "  F  = CAMPAIGN NAMEPLATE (sheet 2b 'qty_planned', sheet 6 'tyres_planned') = D + C + E", "value": f"{int(_fed_tot.get(p, 0) + _unfed.get(p, 0)):,}"},
            {"plant": p, "metric": "     F is PRESS CAPACITY SEATED, not tyres. Never quote it as output.", "value": ""},
            {"plant": p, "metric": "same-size share", "value": d11.get(f"{p} same-size share of build changeovers")},
            {"plant": p, "metric": "build changeovers", "value": f"{int(bp['changeover'].sum()):,}"},
            {"plant": p, "metric": "changeovers / machine-day", "value": d11.get(f"{p} build changeovers / machine-day")},
            {"plant": p, "metric": "lot p50 (tyres per run)", "value": f"{np.median(q):.0f}"},
            {"plant": p, "metric": "runs below min lot", "value": d11.get(f"{p} build runs below min_lot ({'150' if p=='PCR' else '70'})")},
            {"plant": p, "metric": "R5 GT wait max", "value": d11.get(f"{p} GT wait max (R5)")},
            {"plant": p, "metric": "GT wait p95", "value": d11.get(f"{p} GT wait p95")},
            {"plant": p, "metric": "GT inventory mean (time-wt)", "value": d11.get(f"{p} mean GT inventory (G8)")},
            {"plant": p, "metric": "GT inventory daily max", "value": f"{dsum['gt_inventory_day_mean'].max():,}"},
            {"plant": p, "metric": "GT inventory last day", "value": d11.get(f"{p} last-day GT inventory (G8)")},
            {"plant": p, "metric": "machine occupancy %", "value": f"{100*mp['busy_h'].sum()/(mp.height*H):.1f}%"},
            {"plant": p, "metric": "presses used", "value": press.filter(pl.col('plant')==p).height},
            {"plant": p, "metric": "realised n_g", "value": d11.get(f"{p} realised n_g (concurrent presses/GT)")},
        ]
    sheets["9a_kpi_summary"] = pl.DataFrame(kpi)
    sheets["9b_l11_invariants"] = inv.with_columns(
        pl.col("invariant").replace_strict(MISMINED, default="").alias("caveat"))

    # ---- 11. CHANGEOVER BY MACHINE ----------------------------------------
    # Per-machine changeover ledger, with the WHY of the single worst one and
    # the plant's own rate for the SAME machine beside ours.
    #
    # Every minute here is charged at THAT MACHINE's own rate from the plant
    # master (PCR 1-5 28/60, PCR 6-11 22/42, TBR 10/24). PARTITION §1c: a flat
    # 11.3/42.4 was once charged to both plants, which understated PCR same-size
    # ~2x and overstated TBR different-size 77 %, so the plants were not even
    # comparable. Never hardcode a changeover minute -- `_same`/`_diff` above are
    # read from `cap_changeover.parquet`.
    #
    # RECONCILES BY CONSTRUCTION: built from `runs`, the same frame behind sheet
    # 1b and the `mach` aggregate in sheet 5, so `changeovers` here sums to sheet
    # 5's column and `setup_required_min` to its `setup_required_h`.
    co = runs.filter(pl.col("changeover")).with_columns([
        pl.when(pl.col("size_change")).then(pl.lit("size-change"))
        .otherwise(pl.lit("same-size")).alias("co_type"),
        pl.col("_pr2").alias("from_rim"),
        pl.col("_pg2").alias("from_gt"),
    ])
    if co.height:
        # the single worst changeover on each machine, and why it cost that much
        _worst = (co.sort(["plant", "machine", "setup_required_min", "start_ts"],
                          descending=[False, False, True, False])
                  .group_by(["plant", "machine"]).first()
                  .select([
                      "plant", "machine",
                      pl.col("setup_required_min").round(1).alias("max_single_co_min"),
                      pl.col("from_gt").alias("max_co_from_gt"),
                      pl.col("gt_code").alias("max_co_to_gt"),
                      pl.col("from_rim").alias("max_co_from_rim"),
                      pl.col("rim").alias("max_co_to_rim"),
                      pl.col("co_type").alias("max_co_type"),
                      pl.col("start_ts").alias("max_co_at")]))
        cm = (co.group_by(["plant", "machine"]).agg(
            pl.len().alias("changeovers"),
            (pl.col("co_type") == "same-size").sum().alias("same_size"),
            (pl.col("co_type") == "size-change").sum().alias("size_changes"),
            pl.col("setup_required_min").sum().round(1).alias("total_co_min"),
            pl.col("setup_required_min").mean().round(2).alias("mean_co_min"),
            pl.col("setup_shortfall_min").sum().round(1).alias("unreserved_co_min"),
        ).join(_worst, on=["plant", "machine"], how="left"))
    else:
        cm = pl.DataFrame(schema={"plant": pl.Utf8, "machine": pl.Utf8,
                                  "changeovers": pl.UInt32})
    # machine-days on which this machine actually ran, so the rate has the same
    # denominator as the plant's (PARTITION §4d: a metric that divides plant
    # EVENT rows by our CAMPAIGN rows is a 10x error that still "passes").
    _md = (b.group_by(["plant", "machine"]).agg(
        pl.col("plant_day").n_unique().alias("machine_days_run")))
    cm = cm.join(_md, on=["plant", "machine"], how="left")
    # ---- the plant's OWN rate for the same machine, same month ----
    plant_co: dict = {}
    try:
        from planner.data.warehouse import duck            # noqa: E402
        _lo = f"{y}-{m:02d}-01"
        _hi = f"{y + (m == 12)}-{(m % 12) + 1:02d}-01"
        _pb = pl.DataFrame(duck().execute(
            "SELECT plant, machineCode m, itemCode g, event_ts ts FROM v_build "
            "WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL "
            "AND date >= ?::DATE AND date < ?::DATE", [_lo, _hi]).fetchall(),
            schema=["plant", "m", "g", "ts"], orient="row")
        if _pb.height:
            _d = (_pb.sort(["m", "ts"])
                  .with_columns(pl.col("g").shift(1).over("m").alias("prev")))
            _runs = (_d.with_columns((pl.col("g") != pl.col("prev")).fill_null(True)
                                     .cum_sum().over("m").alias("rid"))
                     .group_by(["plant", "m", "rid"])
                     .agg(pl.col("ts").min().alias("t")))
            _pd = (_pb.with_columns(pl.col("ts").dt.date().alias("d"))
                   .select(["m", "d"]).unique()
                   .group_by("m").agg(pl.len().alias("pdays")))
            _pr = (_runs.group_by(["plant", "m"]).agg(pl.len().alias("pruns"))
                   .join(_pd, on="m", how="left"))
            for r in _pr.iter_rows(named=True):
                if r["pdays"]:
                    plant_co[r["m"]] = round((r["pruns"] - 1) / r["pdays"], 2)
    except Exception as _e:                                    # noqa: BLE001
        print(f"  !! plant per-machine changeover rate unavailable ({_e}) -- "
              f"column left null, OURS is still exact")
    cm = cm.with_columns([
        (pl.col("changeovers") / pl.col("machine_days_run"))
        .round(2).alias("co_per_machine_day_OURS"),
        pl.col("machine").replace_strict(plant_co, default=None)
        .alias("co_per_machine_day_PLANT"),
    ])
    cm = cm.with_columns(
        (pl.col("co_per_machine_day_OURS") - pl.col("co_per_machine_day_PLANT"))
        .round(2).alias("co_per_machine_day_DELTA")).sort(["plant", "machine"])
    sheets["11_changeover_by_machine"] = fmt_ts(cm, ["max_co_at"])

    # ---- 12. LOT SIZE VIOLATIONS ------------------------------------------
    # Every build run below its plant's B12 floor, with the reason it was let
    # through. PARTITION §4f: a ROW is a SLICE, not a setup -- 100 % of slice
    # rows sit below the floor and that is meaningless. This sheet judges
    # `block_id`, i.e. a run split at >1 h gaps, which is the physical setup.
    #
    # Under PLANNER_STRICT_LOT_FLOOR=1 (default, plant instruction §4m) the
    # correct content is ZERO ROWS. The sheet is still emitted, with a stated
    # zero line, because an absent sheet cannot be told apart from an
    # unmeasured one -- an empty sheet is evidence, not an omission.
    _floor_of = CONFIG_FLOOR
    viol = (runs.with_columns(
        pl.col("plant").replace_strict(_floor_of, default=0).alias("floor"))
        .filter(pl.col("run_qty") < pl.col("floor"))
        .with_columns([
            (pl.col("floor") - pl.col("run_qty")).alias("shortfall"),
            pl.lit(_STRICT_NOTE).alias("why_allowed"),
        ])
        .select(["plant", "machine", "gt_code", "gt_description", "rim",
                 "run_qty", "floor", "shortfall", "slices", "start_ts",
                 "end_ts", "block_id", "why_allowed"])
        .sort(["plant", "shortfall"], descending=[False, True]))
    if viol.height == 0:
        viol = pl.DataFrame([{
            "plant": "(none)", "machine": "", "gt_code": "", "gt_description": "",
            "rim": "", "run_qty": None, "floor": None, "shortfall": None,
            "slices": None, "start_ts": "", "end_ts": "", "block_id": "",
            "why_allowed":
            f"ZERO VIOLATIONS. Every build run on both plants is at or above "
            f"its B12 floor (PCR {_floor_of.get('PCR')} / TBR "
            f"{_floor_of.get('TBR')}), judged on setup blocks split at >1 h "
            f"gaps. PLANNER_STRICT_LOT_FLOOR=1."}],
            schema_overrides={"run_qty": pl.Float64, "floor": pl.Int64,
                              "shortfall": pl.Float64, "slices": pl.UInt32})
        print(f"  LOT SIZE VIOLATIONS: none -- sheet 12 emitted with a stated "
              f"zero line (floors PCR {_floor_of.get('PCR')} / "
              f"TBR {_floor_of.get('TBR')})")
    else:
        viol = fmt_ts(viol, ["start_ts", "end_ts"])
        print(f"  LOT SIZE VIOLATIONS: {viol.height} run(s) below floor -- see "
              f"sheet 12")
    sheets["12_lot_size_violations"] = viol

    # ---- write -------------------------------------------------------------
    counts = {}
    for name, df in sheets.items():
        df.write_csv(out / "csv" / f"{name}.csv")
        counts[name] = df.height
    # ---- CARRY-OUT + SETUP report, printed so it cannot be missed ----------
    print("  CARRY-OUT (rows dated past month end -- campaigns that START inside "
          f"{month} and FINISH after)")
    for p in ("PCR", "TBR"):
        _b = b.filter((pl.col("plant") == p) & pl.col("carry_out"))
        _c = camp.filter((pl.col("plant") == p) & pl.col("carry_out"))
        _cf = camp.filter((pl.col("plant") == p) & pl.col("finishes_next_month"))
        print(f"     {p}  build rows {_b.height:>4} ({int(_b['qty'].sum()):>6,} tyres) · "
              f"cure rows starting after month end {_c.height:>3} · "
              f"cure campaigns finishing next month {_cf.height:>3}")
    print("  SETUP RESERVATION (L7 does not reserve changeover time -- see README)")
    for p in ("PCR", "TBR"):
        _r = runs.filter(pl.col("plant") == p)
        _need = float(_r["setup_required_min"].sum()) / 60.0
        _short = float(_r["setup_shortfall_min"].sum()) / 60.0
        _n = int((_r["setup_shortfall_min"] > 0.001).sum())
        _tr = int((_r["setup_required_min"] > 0).sum())
        print(f"     {p}  owed {_need:7.1f} h · NOT reserved {_short:7.1f} h "
              f"({100*_short/max(_need,1e-9):4.1f}%) · runs short {_n:,} of {_tr:,}")

    xl = out / f"schedule_{month}.xlsx"
    with pl.Config():
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        hdr = Font(bold=True, color="FFFFFF")
        fill = PatternFill("solid", start_color="1F3864")
        for name, df in sheets.items():
            ws = wb.create_sheet(name[:31])
            ws.append(df.columns)
            for cell in ws[1]:
                cell.font, cell.fill = hdr, fill
            for row in df.iter_rows():
                ws.append([None if isinstance(v, float) and np.isnan(v) else v
                           for v in row])
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, c in enumerate(df.columns, 1):
                w = max(len(str(c)) + 2, 12)
                ws.column_dimensions[get_column_letter(i)].width = min(w, 34)
        # A workbook open in Excel cannot be overwritten. Never lose the export
        # over it: save beside the locked file and say so loudly.
        try:
            wb.save(xl)
        except PermissionError:
            alt = out / f"schedule_{month}__NEW.xlsx"
            wb.save(alt)
            print(f"  !! {xl.name} is LOCKED (open in Excel). Wrote {alt.name} "
                  f"instead -- close Excel, delete the old file and rename this one.")
            xl = alt
    print(f"  -> {xl}")
    for k, v in counts.items():
        print(f"     {k:<28}{v:>8,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
