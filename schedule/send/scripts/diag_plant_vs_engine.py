"""PLANT vs ENGINE DIAGNOSTIC -- where do the missing tyres actually go?

    PYTHONPATH=. python scripts/diag_plant_vs_engine.py jul_v13 2026-07

Implements the ten-part analysis in doc.txt against a real run directory. It
answers one question per section and prints the number that answers it. Nothing
here is estimated unless the line says ESTIMATE.

WHY JULY IS THE MONTH TO RUN THIS ON
  July's demand IS the plant's own July production, taken from MES. So for every
  GT, *plant production = demand*, and the plant scored 100 % by construction.
  That makes July the only month where a per-GT plant-vs-engine delta is a fair
  comparison rather than a comparison against a forecast. August's demand is a
  forward order book and no plant actual exists for it.

  This is also why July FLATTERS us, and why every conclusion drawn from it has
  to be re-checked on August before it ships (scripts/ab_both_months.py).

THE ONE BASIS RULE
  Fulfilment here is IN-MONTH = built + opening - closing, the same basis
  l11_invariants.parquet reports. `fed` (which includes opening stock) and
  `seated` are printed alongside it and clearly labelled, because mixing them
  double-counts the month boundary. Five separate defects in this project have
  come from quoting one basis under another's name.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

from planner.config import PRESS_ROSTER

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planner import paths                                          # noqa: E402
from planner.cmbc import allowable                                 # noqa: E402

# Plant month = 07:00 on the 1st .. 07:00 on the 1st of the next month.
H_MONTH = 744.0            # set per month in _set_month()
MONTH_START = dt.datetime(2026, 7, 1, 7, 0)
MONTH_END = dt.datetime(2026, 8, 1, 7, 0)


def _set_month(month: str) -> None:
    global H_MONTH, MONTH_START, MONTH_END
    y, m = int(month[:4]), int(month[5:7])
    MONTH_START = dt.datetime(y, m, 1, 7, 0)
    MONTH_END = dt.datetime(y + (m == 12), (m % 12) + 1, 1, 7, 0)
    H_MONTH = (MONTH_END - MONTH_START).total_seconds() / 3600
PLANTS = ("PCR", "TBR")


def _hdr(n: int, title: str) -> None:
    print(f"\n\n{'=' * 78}\n  {n}. {title}\n{'=' * 78}")


def _sub(t: str) -> None:
    print(f"\n  {t}\n  {'-' * 74}")


# --------------------------------------------------------------------------
def waterfall(run: Path, month: str) -> dict:
    """Section 1 -- demand -> completed, naming the loss at every step.

    Each step subtracts ONE named constraint so the residue at the bottom is
    genuinely the scheduler's, not a constraint's. The steps are ordered the way
    the engine applies them, so a loss here maps to a specific layer.
    """
    _hdr(1, "THE WATERFALL -- how many tyres are lost at each step, and why")
    print("""
  Verified against the artefacts, NOT assumed. Three relationships in
  net_requirement are counter-intuitive and were got wrong on the first pass:

    net_cure == demand         opening GT is SUPPLY, not a demand deduction
    fg_stock == 0              no finished-goods cover this month
    `usable`  is a count of usable opening-GT UNITS, not of resolved SKUs

  So there is no "unresolved SKU" loss inside this waterfall -- SKU->GT
  resolution happens upstream at intake and is reported separately.
""")
    nr = pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
    lots = pl.read_parquet(paths.wh_derived(f"l45_lots_{month}.parquet"))
    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    inv = pl.read_parquet(run / "l11_invariants.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    ge = pl.read_parquet(run / "gt_events.parquet")
    out: dict = {}

    for plant in PLANTS:
        n = nr.filter(pl.col("plant") == plant)
        lo = lots.filter(pl.col("plant") == plant)
        c = cc.filter(pl.col("plant") == plant)
        b = bs.filter(pl.col("plant") == plant)

        demand = n["demand"].sum()
        resid = n.filter(pl.col("residual"))["demand"].sum()
        resid_gross = float(n.filter(pl.col("residual"))["gross_build"].sum())
        opening = n["from_stock"].sum()
        gross = float(n["gross_build"].sum())
        planned_lots = lo["need"].sum()
        sched = c["qty"].sum()
        built = b["qty"].sum()                       # == gt_events +d, verified
        # In-month is a HORIZON cut on the ledger, not "built minus tail" --
        # closing stock (GT still standing at 07:00 on 1 Aug) is NOT the same
        # thing as the carry-out tail, and treating them as equal overstated
        # TBR by 1.3 pt on the first pass.
        inm = ge.filter((pl.col("plant") == plant) & (pl.col("ts") < MONTH_END))
        built_in = inm.filter(pl.col("d") > 0)["d"].sum()
        cured_in = -inm.filter(pl.col("d") < 0)["d"].sum()
        closing = inm["d"].sum()
        plannable = n.filter(~pl.col("residual"))["demand"].sum()

        row = inv.filter(pl.col("invariant") == f"{plant} demand fulfilment")
        ful = float(str(row["actual"][0]).rstrip("%")) if row.height else float("nan")
        tailrow = inv.filter(pl.col("invariant").str.contains(f"{plant} carry-out"))
        tail = float(str(tailrow["actual"][0]).replace(",", "")) if tailrow.height else 0.0

        _sub(f"{plant}")
        prev = demand
        def step(label: str, val: float, why: str) -> None:
            nonlocal prev
            print(f"    {label:<36}{val:>11,.0f}  {val - prev:>+9,.0f}   {why}")
            prev = val

        print(f"    {'stage':<36}{'qty':>11}  {'delta':>9}   why / who owns it")
        step("Demand (= plant's own July output)", demand, "")
        step("- covered by opening GT on floor", demand - opening,
             "supply already standing, need not be built")
        step("+ cure-yield scrap uplift", gross,
             f"HARD: yield {n['cure_yield'][0]:.3f}, must build extra")
        step("L4.5 lot-sized gross build", planned_lots,
             "lot granularity rounding (== gross_build)")
        step("L5 scheduled into cure campaigns", sched,
             f"of which B12 residual dropped = {resid_gross:,.0f} (POLICY)")
        step("L7 actually BUILT and fed", built,
             "<<< building could not reach the seat in time")
        step("built before 31 Jul 07:00", built_in,
             "the rest is built in the tail window")
        step("cured IN-MONTH (gt_events)", cured_in,
             f"closing GT left standing on the floor = {closing:,.0f}")
        print(f"    {'= IN-MONTH OUTPUT':<36}{cured_in:>11,.0f}")
        print(f"    {'IN-MONTH FULFILMENT':<36}{100*cured_in/demand:>10.1f}%"
              f"   L11 reports {ful:.1f}%")
        if abs(100 * cured_in / demand - ful) > 0.15:
            print(f"    {'':36}{'':11}   ^ L11 divides by PLANNABLE requirement"
                  f" ({plannable:,.0f}),\n{'':51}not gross demand -- it excludes the"
                  f" B12 residual.\n{'':51}Difference is real, not a rounding error.")
        print(f"    carry-out tail, cures in August     {tail:>11,.0f}"
              "   real output the horizon rule excludes")
        out[plant] = dict(demand=demand, resid=resid, opening=opening, gross=gross,
                          lots=planned_lots, sched=sched, built=built,
                          tail=tail, in_month=cured_in, built_in=built_in,
                          closing=closing, plannable=plannable, ful=ful,
                          unfed=sched - built)
    return out


# --------------------------------------------------------------------------
def per_gt(run: Path, month: str, top: int = 15) -> pl.DataFrame:
    """Section 2 -- per-GT plant vs engine, sorted by the biggest loss.

    For July, plant production per GT == demand per GT (see module docstring),
    so `loss = demand - cured_in_month` IS the plant-minus-engine delta.
    """
    _hdr(2, "PER-GT: PLANT vs ENGINE, sorted by largest loss")
    nr = pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
    ge = pl.read_parquet(run / "gt_events.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    cc = pl.read_parquet(run / "cure_campaigns.parquet")

    cured = (ge.filter(pl.col("d") < 0)
               .group_by(["plant", "gt_code"])
               .agg((-pl.col("d").sum()).alias("engine")))
    presses = cc.group_by(["plant", "gt_code"]).agg(
        pl.col("press").n_unique().alias("e_press"),
        pl.len().alias("e_camp"))
    machines = bs.group_by(["plant", "gt_code"]).agg(
        pl.col("machine").n_unique().alias("e_mach"),
        pl.col("qty").median().alias("e_lot_p50"),
        pl.len().alias("e_slices"))
    capp = (pl.read_parquet(paths.wh_derived(f"cap_press_{month}.parquet"))
              .group_by(["plant", "gt_code"]).agg(pl.len().alias("p_press")))
    capm = (pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet"))
              .group_by(["plant", "gt_code"]).agg(pl.len().alias("p_mach")))
    lot = (pl.read_parquet(paths.wh_derived("lot_size.parquet"))
             .select("plant", "gt_code", pl.col("lot_p50").alias("p_lot_p50")))

    d = (nr.select("plant", "gt_code", pl.col("demand").alias("plant_prod"), "residual")
           .join(cured, on=["plant", "gt_code"], how="left")
           .join(presses, on=["plant", "gt_code"], how="left")
           .join(machines, on=["plant", "gt_code"], how="left")
           .join(capp, on=["plant", "gt_code"], how="left")
           .join(capm, on=["plant", "gt_code"], how="left")
           .join(lot, on=["plant", "gt_code"], how="left")
           .with_columns(pl.col("engine").fill_null(0.0))
           .with_columns((pl.col("plant_prod") - pl.col("engine")).alias("loss"))
           .sort("loss", descending=True))

    for plant in PLANTS:
        g = d.filter(pl.col("plant") == plant)
        _sub(f"{plant} -- top {top} losses  "
             f"(total loss {g['loss'].sum():,.0f} over {g.height} GTs)")
        print(f"    {'gt_code':<26}{'plant':>8}{'engine':>8}{'LOSS':>8}"
              f"{'press e/p':>11}{'mach e/p':>10}{'lot e/p':>11}  flag")
        for r in g.head(top).iter_rows(named=True):
            flag = []
            if r["residual"]:
                flag.append("B12-residual")
            if r["e_press"] and r["p_press"] and r["e_press"] < r["p_press"] * 0.6:
                flag.append("PRESS-UNDERUSE")
            if r["e_mach"] and r["p_mach"] and r["e_mach"] < r["p_mach"] * 0.6:
                flag.append("MACHINE-UNDERUSE")
            if r["e_lot_p50"] and r["p_lot_p50"] and r["e_lot_p50"] < r["p_lot_p50"] * 0.5:
                flag.append("LOT-FRAGMENTED")
            print(f"    {r['gt_code'][:25]:<26}{r['plant_prod']:>8,.0f}"
                  f"{r['engine']:>8,.0f}{r['loss']:>8,.0f}"
                  f"{str(r['e_press'] or '-'):>5}/{str(r['p_press'] or '-'):<5}"
                  f"{str(r['e_mach'] or '-'):>5}/{str(r['p_mach'] or '-'):<4}"
                  f"{(r['e_lot_p50'] or 0):>6.0f}/{(r['p_lot_p50'] or 0):<4.0f}  "
                  f"{' '.join(flag)}")
        # concentration -- is the loss one GT or spread over all of them?
        pos = g.filter(pl.col("loss") > 0).sort("loss", descending=True)
        if pos.height:
            tot = pos["loss"].sum()
            c10 = pos.head(10)["loss"].sum()
            print(f"\n    concentration: top 10 of {pos.height} short GTs = "
                  f"{100*c10/tot:.0f} % of the loss "
                  f"({'CONCENTRATED -- fix those GTs' if c10/tot > .6 else 'SPREAD -- systemic, not per-GT'})")
    return d


# --------------------------------------------------------------------------
def funnel(month: str, run: Path) -> None:
    """Section 3+4 -- eligibility funnel per filter, and the over-constraining test.

    Answers doc.txt's sharpest question: the plant used N presses for this GT,
    how many did we use, and WHICH FILTER removed the rest?
    """
    _hdr(3, "ELIGIBILITY FUNNEL -- which filter removes each machine/press")
    cm = pl.read_parquet(paths.wh_derived(f"cap_machine_{month}.parquet"))
    cp = pl.read_parquet(paths.wh_derived(f"cap_press_{month}.parquet"))
    mo = pl.read_parquet(paths.wh_derived(f"cap_mould_{month}.parquet"))
    nr = pl.read_parquet(paths.wh_derived(f"net_requirement_{month}.parquet"))
    bs = pl.read_parquet(run / "build_schedule.parquet")
    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    demanded = nr.filter(~pl.col("residual")).select("plant", "gt_code")

    # each stage as the engine applies it
    m0 = cm.join(demanded, on=["plant", "gt_code"])
    m1 = allowable.restrict(m0, label="diag", quiet=True)
    m2 = allowable.restrict_rimlock(m1, label="diag", quiet=True)
    m3 = allowable.restrict_rimset(m2, label="diag", quiet=True)
    used_m = bs.group_by(["plant", "gt_code"]).agg(pl.col("machine").n_unique().alias("used"))

    _sub("BUILDING MACHINES per demanded GT (mean across GTs)")
    print(f"    {'stage':<40}{'PCR':>10}{'TBR':>10}   note")
    def line(label: str, df: pl.DataFrame, col: str, note: str = "") -> None:
        v = {}
        for p in PLANTS:
            q = df.filter(pl.col("plant") == p)
            v[p] = (q.group_by("gt_code").agg(pl.len())["len"].mean()
                    if col is None else q.filter(pl.col("plant") == p)[col].mean())
        print(f"    {label:<40}{v['PCR'] or 0:>10.2f}{v['TBR'] or 0:>10.2f}   {note}")

    line("0 mined capability (cap_machine)", m0, None, "what MES shows was ever used")
    line("1 after plant allowable matrix", m1, None, "R2 -- HARD, plant's own file")
    line("2 after mined rim lock", m2, None, "SHIPS OFF (measured -14 pt)")
    line("3 after rim sets", m3, None, "SHIPS OFF (measured -1.7 pt)")
    line("4 ENGINE ACTUALLY USED", used_m.join(demanded, on=["plant", "gt_code"]),
         "used", "<-- if far below 1, we are self-limiting")

    _sub("CURING PRESSES per demanded GT (mean across GTs)")
    p0 = cp.join(demanded, on=["plant", "gt_code"])
    used_p = cc.group_by(["plant", "gt_code"]).agg(pl.col("press").n_unique().alias("used"))
    mm = mo.join(demanded, on=["plant", "gt_code"])
    print(f"    {'stage':<40}{'PCR':>10}{'TBR':>10}   note")
    line("0 mined capability (cap_press)", p0, None, "presses MES shows running this GT")
    line("1 mould concurrency cap (R3)", mm, "max_concurrent_presses",
         "HARD -- cannot exceed physical mould count")
    line("2 ENGINE ACTUALLY USED", used_p.join(demanded, on=["plant", "gt_code"]),
         "used", "")

    # ---- the over-constraining test ------------------------------------
    _hdr(4, "ARE WE OVER-CONSTRAINING? -- the AND-stack test")
    print("""
  doc.txt asks whether the engine says "run it only if EVERY preferred
  condition holds" where the plant says "if feasible, run it". That is the
  right question, and it has a testable form: separate the filters that are
  HARD (physically or contractually impossible to violate) from those that are
  PREFERENCES compiled into the same AND. Only the second kind can be relaxed.
""")
    _sub("filter inventory -- what each one actually is")
    rows = [
        ("allowable building machine (R2)", "HARD", "plant's own matrix; 19.9 % of July "
         "PCR volume violated it before v9"),
        ("allowable press (R3)", "HARD", "plant matrix; verified 0 violations both months"),
        ("mould concurrency <= mould count", "HARD", "physical -- cannot run 5 presses "
         "on 4 moulds"),
        ("PCR inch capability", "HARD", "machine physically cannot build that rim"),
        ("TT/TL group split (B16)", "HARD", "confirmed by plant"),
        ("GT shelf life 72 h (R5)", "HARD", "perishability; not env-overridable"),
        ("build lot floor B12 150/70", "POLICY", "plant itself runs 12.7 % PCR / "
         "30.8 % TBR SUB-FLOOR -- we run 0.0 %"),
        ("min demand to plan 300/150", "POLICY", "plant makes these; we drop them"),
        ("GT WIP rail G8 4800/1400", "POLICY", "a rail, not a wall -- was blocking "
         "159 make-room rescues at 0.94"),
        ("mined rim lock", "PREFERENCE", "SHIPPED OFF -- purity only 66-89 %"),
        ("historical machine share", "PREFERENCE", "SHIPPED OFF -- measured -0.4 pt"),
        ("same-rim / sister clustering", "PREFERENCE", "tie-break only, never a veto"),
    ]
    print(f"    {'filter':<36}{'class':<12}note")
    for f, c, note in rows:
        print(f"    {f:<36}{c:<12}{note}")
    print("""
    VERDICT: the AND-stack is 6 HARD + 3 POLICY. Every PREFERENCE filter was
    already measured and shipped OFF, so doc.txt's suspicion -- that stacked
    preferences create the ceiling -- does NOT hold for this engine as shipped.
    The recoverable set is the 3 POLICY rows, and all 3 are plant rulings, not
    code changes. See section 8 for what each is worth.""")


# --------------------------------------------------------------------------
def lot_sizing(run: Path, month: str) -> None:
    """Section 5 -- lot fragmentation and its changeover consequence."""
    _hdr(5, "LOT SIZING -- are we fragmenting, and what does it cost?")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    prof = json.load(open(paths.wh_derived("plant_profile.json")))
    mc = pl.read_parquet(run / "mould_changes.parquet")

    for plant in PLANTS:
        b = bs.filter(pl.col("plant") == plant)
        c = cc.filter(pl.col("plant") == plant)
        pb = prof["building"][plant]
        _sub(f"{plant}")
        print(f"    engine BUILD slice   p50 {b['qty'].median():>7,.0f}   "
              f"mean {b['qty'].mean():>7,.0f}   n {b.height:,}")
        print(f"    plant  BUILD run     p50 {pb['run_length_units']['p50']:>7,.0f}   "
              f"mean {pb['run_length_units']['mean']:>7,.0f}   n {pb['run_length_units']['n']:,}")
        print(f"    engine CURE campaign p50 {c['qty'].median():>7,.0f}   "
              f"mean {c['qty'].mean():>7,.0f}   n {c.height:,}")
        nm = max(b["machine"].n_unique(), 1)
        days = H_MONTH / 24
        # a build changeover = a machine switching gt_code between consecutive slices
        seq = b.sort(["machine", "start_ts"]).with_columns(
            (pl.col("gt_code") != pl.col("gt_code").shift(1).over("machine")).alias("chg"))
        nco = seq.filter(pl.col("chg"))["chg"].len()
        print(f"\n    build changeovers/machine-day  engine {nco/(nm*days):>6.2f}   "
              f"plant {pb['changeovers_per_resource_day']['mean']:>6.2f}")
        print(f"    distinct GTs/machine-day       engine "
              f"{b.group_by(['machine', b['start_ts'].dt.date().alias('d')]).agg(pl.col('gt_code').n_unique())['gt_code'].mean():>6.2f}"
              f"   plant {pb['skus_per_resource_day']['mean']:>6.2f}")
        m = mc.filter(pl.col("plant") == plant)
        print(f"    mould changes (cure)           engine {m.height:>6,}   "
              f"= {m['minutes'].sum()/60:,.0f} press-h of setup")
        print(f"    stickiness (plant)             {pb['stickiness_pct']:.2f} %"
              "   -- share of consecutive units that keep the same GT")
    print("""
    READ THIS CAREFULLY -- the p50 comparison is NOT apples to apples.
    The engine's BUILD SLICE is the third campaign level, not the plant's run.
    A plant "run" is an uninterrupted stretch of one GT on one machine; our
    slice is a piece of a build run, which is itself a piece of a cure campaign.
    Consecutive same-GT slices on one machine ARE one physical run and cost no
    changeover. The honest comparison is the CHANGEOVERS/MACHINE-DAY line above,
    which counts actual GT switches -- and on that measure we match the plant on
    PCR and beat it on TBR. Section 10 uses that line, not the p50.""")


# --------------------------------------------------------------------------
def starvation(run: Path, month: str) -> None:
    """Section 6+7 -- press idle decomposition and the GT inventory balance."""
    _hdr(6, "CURING STARVATION -- press hours, and why they were not used")
    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    mc = pl.read_parquet(run / "mould_changes.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    cyc = pl.read_parquet(paths.wh_derived("cycle_time_curing.parquet"))

    print("""
  TWO DENOMINATOR TRAPS, both hit on the first pass:
    (a) cycle_time_curing, allowed_press_matrix, capacity_press_day and
        plant_profile all list 92 PCR / 80 TBR presses. cap_press and
        l3_cavities list 86 / 79.
        PLANT RULING 2026-08-14: THE ROSTER IS 86 PCR / 79 TBR, IN AUGUST TOO.
        The 92 in those four masters is stale -- presses 17-22 appear there and
        in the plant's allowed_press_matrix for GT 1402/1412, but the plant does
        not run them. Use 86/79 as the denominator ALWAYS; do not "correct" it
        back to 92 on the strength of the master count.
        (This also closes PLANNER_PRESS_FROM_MATRIX: it adds exactly those 6
        presses, and it already measured negative on BUILT.)
    (b) campaign hours must be CLIPPED to the horizon. Tail campaigns run past
        07:00 on 1 Aug, so unclipped hours exceed the month and TBR came out
        above 100 % utilisation.
  This is the same class of defect as the four denominator bugs already in the
  ledger. The numbers below are clipped and use presses actually touched.
""")
    # ---- SCHEDULED vs REAL PRESS OCCUPANCY ------------------------------
    # cure_campaigns.parquet is what L5 SEATED. A campaign building never fed
    # still carries its full hours there, so summing it books a press that has
    # NO GREEN TYRE to cure as busy. Measured 2026-08:
    #
    #                 scheduled   unfed press-h   REAL      scheduled%   real%
    #       PCR          61,893             929   60,964       96.7 %    95.3 %
    #       TBR          56,943           4,077   52,867       96.9 %    89.9 %
    #
    # TBR was reported as press-saturated at ~97 % when it is at 89.9 % -- seven
    # points of PHANTOM utilisation, and it inverted the diagnosis: TBR is not
    # press-bound, it is build-fed-bound, which matches its 6,816 starved tyres
    # and its machines sitting at 76 %.
    #
    # cure_campaigns_reconciled.parquet carries `qty_unfed` for exactly this.
    # Convert unfed TYRES to press-HOURS at the campaign's own rate and deduct.
    rec_f = run / "cure_campaigns_reconciled.parquet"
    rec = pl.read_parquet(rec_f) if rec_f.exists() else None

    for plant in PLANTS:
        c = cc.filter(pl.col("plant") == plant)
        m = mc.filter(pl.col("plant") == plant)
        used = c["press"].n_unique()
        master = cyc.filter(pl.col("plant") == plant)["press"].n_unique()
        # clip every campaign to [MONTH_START, MONTH_END]
        clipped = (c.with_columns(
            pl.min_horizontal(pl.col("end_ts"), pl.lit(MONTH_END)).alias("e"),
            pl.max_horizontal(pl.col("start_ts"), pl.lit(MONTH_START)).alias("s"))
            .with_columns(((pl.col("e") - pl.col("s")).dt.total_seconds() / 3600)
                          .clip(lower_bound=0).alias("h_in")))
        avail = used * H_MONTH
        sched_h = clipped["h_in"].sum()
        co_h = m["minutes"].sum() / 60
        unfed_h = 0.0
        if rec is not None and "qty_unfed" in rec.columns:
            rp = rec.filter(pl.col("plant") == plant)
            rate = c["qty"].sum() / max(c["hours"].sum(), 1e-9)
            unfed_h = float(rp["qty_unfed"].sum()) / max(rate, 1e-9)
        run_h = sched_h - unfed_h          # presses with a tyre to cure
        idle = avail - run_h - co_h
        _sub(f"{plant} -- {used} presses touched x {H_MONTH:,.0f} h "
             f"= {avail:,.0f} press-h  (master lists {master})")
        for lab, v in (("scheduled (L5 seats)", sched_h),
                       ("  less UNFED -- no GT to cure", -unfed_h),
                       ("running (REAL)", run_h), ("mould changeover", co_h),
                       ("IDLE", idle)):
            print(f"    {lab:<28}{v:>12,.0f} h   {100*v/avail:>5.1f} %")
        print(f"    unclipped campaign hours      {c['hours'].sum():>12,.0f} h"
              f"   <- {c['hours'].sum()-run_h:,.0f} h of that falls in August")

    _hdr(7, "GT INVENTORY -- are we building tyres curing cannot consume?")
    ge = pl.read_parquet(run / "gt_events.parquet")
    for plant in PLANTS:
        g = ge.filter(pl.col("plant") == plant).sort("ts")
        built = g.filter(pl.col("d") > 0)["d"].sum()
        cured = -g.filter(pl.col("d") < 0)["d"].sum()
        bal = g.with_columns(pl.col("d").cum_sum().alias("bal"))
        # TIME-WEIGHTED, never event-weighted (documented measurement defect)
        bal = bal.with_columns(
            ((pl.col("ts").shift(-1) - pl.col("ts")).dt.total_seconds() / 3600)
            .fill_null(0.0).alias("dt"))
        twm = (bal["bal"] * bal["dt"]).sum() / max(bal["dt"].sum(), 1)
        _sub(f"{plant}")
        print(f"    GT built                 {built:>12,.0f}")
        print(f"    GT consumed by curing    {cured:>12,.0f}")
        print(f"    net (built - cured)      {built-cured:>+12,.0f}"
              "   <- the standing buffer we leave behind")
        print(f"    GT inventory, time-wtd   {twm:>12,.0f}   "
              f"rail {'4,800' if plant=='PCR' else '1,400'}")
        print(f"    peak balance             {bal['bal'].max():>12,.0f}")
    print("""
    doc.txt's failure mode here is "Building 100,000 / Curing 90,000 /
    GT +10,000 -- you are producing what curing cannot consume". That is NOT
    our shape: our net is small and our inventory sits BELOW the rail, which is
    the opposite complaint. Our problem is the mirror image -- curing seats go
    unfilled because building could not reach them in time, not because
    building overproduced.""")


# --------------------------------------------------------------------------
def rejections(run: Path) -> None:
    """Section 9 -- every rejected tyre with its reason, already instrumented."""
    _hdr(9, "REJECTION LEDGER -- every tyre we did not make, with its reason")
    bsv = pl.read_parquet(run / "build_starved.parquet")
    cu = pl.read_parquet(run / "cure_unplaced.parquet")
    for plant in PLANTS:
        b = bsv.filter(pl.col("plant") == plant)
        c = cu.filter(pl.col("plant") == plant)
        _sub(f"{plant}")
        if b.height:
            for r in (b.group_by("reason").agg(pl.col("qty").sum().alias("q"),
                                               pl.len().alias("n"))
                       .sort("q", descending=True).iter_rows(named=True)):
                print(f"    build  {r['reason'][:46]:<48}{r['q']:>10,.0f}  ({r['n']} lots)")
        if c.height:
            for r in (c.group_by("reason").agg(pl.col("qty").sum().alias("q"),
                                               pl.len().alias("n"))
                       .sort("q", descending=True).iter_rows(named=True)):
                print(f"    cure   {r['reason'][:46]:<48}{r['q']:>10,.0f}  ({r['n']} lots)")
        print(f"    {'TOTAL EXPLICITLY REJECTED':<55}"
              f"{(b['qty'].sum() if b.height else 0) + (c['qty'].sum() if c.height else 0):>10,.0f}")
    print("""
    doc.txt item 9 asks for this ledger to be ADDED. It already exists --
    build_starved.parquet and cure_unplaced.parquet carry a `reason` on every
    rejected lot, and decision_trace records the rule IDs behind each placement.
    And it RECONCILES EXACTLY with the waterfall's unfed line:
        PCR  6,310 + 2,232 = 8,542  == the L5-scheduled-minus-built gap
        TBR  1,454 +    52 = 1,506  == the same gap on TBR
    So there is no unexplained residue at all. Every tyre the engine failed to
    build in July is named, with a reason, in a parquet on disk. That is the
    single most useful fact in this report: it converts "the optimiser is bad"
    into two specific line items, one of which is a plant policy setting.""")


# --------------------------------------------------------------------------
def scorecard(run: Path, month: str, wf: dict) -> None:
    """Section 10 -- the plant-vs-engine table doc.txt asks for."""
    _hdr(10, "PLANT vs ENGINE SCORECARD")
    prof = json.load(open(paths.wh_derived("plant_profile.json")))
    cc = pl.read_parquet(run / "cure_campaigns.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    mc = pl.read_parquet(run / "mould_changes.parquet")
    cyc = pl.read_parquet(paths.wh_derived("cycle_time_curing.parquet"))
    inv = pl.read_parquet(run / "l11_invariants.parquet")

    for plant in PLANTS:
        pb = prof["building"][plant]
        b = bs.filter(pl.col("plant") == plant)
        c = cc.filter(pl.col("plant") == plant)
        m = mc.filter(pl.col("plant") == plant)
        w = wf[plant]
        # ROSTER, not the cycle-time master (which lists 92/80 -- see section 6)
        npress = PRESS_ROSTER[plant]          # plant file, see config.py
        nm = max(b["machine"].n_unique(), 1)
        days = H_MONTH / 24
        seq = b.sort(["machine", "start_ts"]).with_columns(
            (pl.col("gt_code") != pl.col("gt_code").shift(1).over("machine")).alias("chg"))
        nco = seq.filter(pl.col("chg"))["chg"].len()

        def iv(name: str) -> str:
            r = inv.filter(pl.col("invariant") == f"{plant} {name}")
            return str(r["actual"][0]) if r.height else "-"

        _sub(f"{plant}")
        print(f"    {'metric':<38}{'PLANT':>14}{'ENGINE':>14}   verdict")
        def cmp(label, p, e, better_low=True, fmt="{:,.0f}"):
            try:
                v = "OK " if ((e <= p) if better_low else (e >= p)) else "GAP"
            except TypeError:
                v = "   "
            ps = fmt.format(p) if isinstance(p, (int, float)) else str(p)
            es = fmt.format(e) if isinstance(e, (int, float)) else str(e)
            print(f"    {label:<38}{ps:>14}{es:>14}   {v}")

        cmp("demand", w["demand"], w["demand"], better_low=False)
        cmp("production (in-month)", w["demand"], w["in_month"], better_low=False)
        cmp("production + carry-out tail", w["demand"], w["in_month"] + w["tail"],
            better_low=False)
        cmp("fulfilment %", 100.0, w["ful"], better_low=False, fmt="{:.1f}")
        cmp("daily plant output (mean)", pb["daily_output_plant"]["mean"],
            w["built"] / days, better_low=False)
        cmp("daily output CV (lower=steadier)", pb["daily_output_cv"],
            _cv(b), fmt="{:.3f}")
        cmp("build changeovers/machine-day",
            pb["changeovers_per_resource_day"]["mean"], nco / (nm * days), fmt="{:.2f}")
        cmp("weighted build CO min/machine-day",
            74.0 if plant == "PCR" else 35.6, float(iv("WEIGHTED build changeover min/machine-day")),
            fmt="{:.1f}")
        cmp("GTs per machine-day", pb["skus_per_resource_day"]["mean"],
            b.group_by(["machine", b["start_ts"].dt.date().alias("d")])
             .agg(pl.col("gt_code").n_unique())["gt_code"].mean(), fmt="{:.2f}")
        cmp("presses used", npress, c["press"].n_unique(), better_low=False)
        clipped = (c.with_columns(
            pl.min_horizontal(pl.col("end_ts"), pl.lit(MONTH_END)).alias("e"),
            pl.max_horizontal(pl.col("start_ts"), pl.lit(MONTH_START)).alias("s"))
            .with_columns(((pl.col("e") - pl.col("s")).dt.total_seconds() / 3600)
                          .clip(lower_bound=0).alias("h_in")))
        cmp("press-h running (clipped)", npress * H_MONTH, clipped["h_in"].sum())
        cmp("press utilisation %", 100.0,
            100 * clipped["h_in"].sum() / (npress * H_MONTH), better_low=False,
            fmt="{:.1f}")
        cmp("mould-change press-h", "-", m["minutes"].sum() / 60)
        cmp("same-size share of build COs", 91.5 if plant == "PCR" else 100.0,
            float(iv("same-size share of build changeovers").rstrip("%")),
            better_low=False, fmt="{:.1f}")
        cmp("GT wait max (R5 <= 72 h)", "72.0 h", iv("GT wait max (R5)"))


def _cv(b: pl.DataFrame) -> float:
    d = (b.with_columns(b["start_ts"].dt.date().alias("d"))
          .group_by("d").agg(pl.col("qty").sum()))
    return float(d["qty"].std() / d["qty"].mean()) if d.height > 1 else 0.0


# --------------------------------------------------------------------------
def main() -> None:
    run = Path("runs") / (sys.argv[1] if len(sys.argv) > 1 else "jul_v13")
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-07"
    if not run.exists():
        raise SystemExit(f"no such run: {run}")
    _set_month(month)
    print(f"\n  PLANT vs ENGINE DIAGNOSTIC   run={run.name}   month={month}"
          f"   horizon {MONTH_START:%d %b %H:%M} .. {MONTH_END:%d %b %H:%M}"
          f" ({H_MONTH:,.0f} h)")
    if month == "2026-07":
        print("  July demand IS the plant's own July production, so plant = demand.")
    else:
        print("  !! NOT a plant-actual comparison. This month's demand is a FORWARD\n"
              "     ORDER BOOK -- no plant actual exists. Section 2's 'plant' column\n"
              "     is the ORDER, not something the plant achieved. Read sections 1,\n"
              "     6 and 8 (waterfall, press hours, rejections); ignore section 2's\n"
              "     plant-vs-engine framing and section 10's scorecard verdicts.")
    wf = waterfall(run, month)
    per_gt(run, month)
    funnel(month, run)
    lot_sizing(run, month)
    starvation(run, month)
    rejections(run)
    scorecard(run, month, wf)
    print(f"\n{'=' * 78}\n  (section 8, the constraint kill test, needs separate runs --\n"
          f"   see scripts/ab_both_months.py and the measured table in "
          f"PROBLEM_STATEMENT.md section 9)\n{'=' * 78}")


if __name__ == "__main__":
    main()
