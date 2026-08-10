"""L2 -- CAPABILITY MODEL.  What CAN run where, and what it costs to switch.

    python -m planner.cmbc.l2_capability --month 2026-07

Emits four tables to warehouse/derived/:
  cap_machine_<month>.parquet   (plant, gt_code, machine, basis, tier, penalty)
  cap_press_<month>.parquet     (plant, gt_code, press, basis)
  cap_mould_<month>.parquet     (plant, gt_code, moulds, max_concurrent_presses)
  cap_changeover.parquet        (plant, machine, machine_type, same_min, diff_min, crew)

ELIGIBILITY IS A UNION WITH PROVENANCE, NEVER AN INTERSECTION
  No single source is both complete and authoritative:
    * the TBR allowable matrix is plant-owned but covers 96% of planned GTs
    * observed production covers 100% but encodes habit, not permission
    * PCR has no eligibility matrix at all -- only an inch-range capability
      statement in the CTP setup workbook
  Taking the intersection deletes feasible production (that once cost 12,172
  tyres). Taking the union without provenance launders habit into permission.
  So every (gt, machine) pair carries a BASIS and a PENALTY, and the optimiser
  prices the weak ones instead of the model silently forbidding or permitting.

  basis        meaning                                    penalty
  BOTH         in the matrix AND observed running          0
  CERTIFIED    in the matrix, never observed               0
  OBSERVED     observed running, absent from the matrix    priced (matrix stale?)
  INCH         within the machine's declared inch range    priced (PCR only)

DEDICATION IS PRICED, NOT ENFORCED
  PCR building has HHI 1.00 -- every GT ran on exactly one machine for 8 months.
  Measured against the inch-capability statement, 56 of 93 GTs are locked to one
  machine while capable on several: 433,798 tyres, 16.3% of volume. That is
  HABIT, not physics. Hard-coding it would cap the plant at its current
  throughput forever, so it enters as a tier-8 penalty the optimiser can buy out
  and must report when it does.
"""
from __future__ import annotations

import argparse
import os
import re
import warnings
from pathlib import Path

import polars as pl

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT.parent.parent
D = ROOT / "warehouse" / "derived"
INP = SRC / "INPUT" / "derived"

# PCR inch capability, read off CTP Set up ...xlsx -> [PCR BUILDING].
# Machines 6-11's upper bound is TRUNCATED in the source; 18 is inferred from
# production and must be confirmed. Confirming it can only ADD capability.
PCR_INCH = {1: (12, 20), 2: (12, 20), 3: (12, 16), 4: (12, 16), 5: (12, 16),
            6: (13, 18), 7: (13, 18), 8: (13, 18), 9: (13, 18),
            10: (13, 18), 11: (13, 18)}
PCR_INCH_SRC = {n: ("spec" if n <= 5 else "spec-truncated,18-inferred")
                for n in PCR_INCH}
# per-machine changeover, from the workbook (the CSV wrongly applies BJ to all)
PCR_TYPE = {n: ("BJ" if n <= 5 else "CONTI") for n in range(1, 12)}
CHG = {"BJ": (28, 60, 3), "CONTI": (22, 42, 3), "TBR": (10, 24, 3)}
TBR_MAKE = {1: "SAV", 2: "SAV", 3: "SAV",
            4: "MESNAC", 5: "MESNAC", 6: "MESNAC",
            7: "MESNAC", 8: "MESNAC", 9: "MESNAC"}
DEDICATION_PENALTY = 10_000      # tier 8, PCR near-hard
OBSERVED_PENALTY = 200           # matrix may be stale -- price, do not forbid
# INCH is a CAPABILITY statement, not a policy. The ranges overlap so heavily
# (1-2: 12-20, 3-5: 12-16, 6-11: 13-18) that rims 13-16 match ALL ELEVEN
# machines -- 9.75 options per GT, and the option set's rim purity collapses to
# 20%. The plant's own 8-month behaviour is 1.7 machines/GT at ~95% purity, and
# no scheduler can produce 92% same-size changeovers from a 20%-pure option set.
#
# Priced ABOVE the tier-8 dedication break so an inch-only pair is taken only
# when nothing demonstrated exists. NOT deleted: 4 PCR GTs have no non-INCH
# machine at all and would strand.
INCH_PENALTY = 5_000


def rim_num(x: str | None) -> int | None:
    if not x:
        return None
    m = re.search(r"(\d{2})", str(x))
    return int(m.group(1)) if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()
    from planner.data.warehouse import duck, set_cutoff
    set_cutoff(None)

    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")
    uni = dem.select(["plant", "gt_code"]).unique()
    sz = pl.read_parquet(INP / "gt_size.parquet")
    gt_rim = {r["gt_code"]: rim_num(r["rim"])
              for r in sz.iter_rows(named=True) if r.get("gt_code")}
    for r in dem.join(sz.select(["sku", "rim"]), on="sku", how="left").iter_rows(named=True):
        if r["rim"] and r["gt_code"] not in gt_rim:
            gt_rim[r["gt_code"]] = rim_num(r["rim"])

    print("=" * 92)
    print(f"L2  CAPABILITY MODEL  --  {a.month}")
    print("=" * 92)

    # ---- machine eligibility -------------------------------------------
    obs = duck().execute(f"""
        SELECT DISTINCT plant, itemCode AS gt_code, machineCode AS machine
        FROM v_build WHERE stage=2 AND itemCode IS NOT NULL
          AND machineCode IS NOT NULL AND date < DATE '{a.month}-01'""").pl()
    cert = pl.read_parquet(INP / "tbr_machine_certified.parquet").select(
        ["mes_gt", "machine"]).unique().rename({"mes_gt": "gt_code"}).with_columns(
        pl.lit("TBR").alias("plant"))

    rows: list[dict] = []
    seen: set[tuple] = set()
    for r in cert.iter_rows(named=True):
        k = (r["plant"], r["gt_code"], r["machine"])
        seen.add(k)
        rows.append({**dict(zip(["plant", "gt_code", "machine"], k)),
                     "basis": "CERTIFIED", "penalty": 0})
    for r in obs.iter_rows(named=True):
        k = (r["plant"], r["gt_code"], r["machine"])
        if k in seen:
            for x in rows:
                if (x["plant"], x["gt_code"], x["machine"]) == k:
                    x["basis"] = "BOTH"
                    break
        else:
            seen.add(k)
            rows.append({"plant": k[0], "gt_code": k[1], "machine": k[2],
                         "basis": "OBSERVED", "penalty": OBSERVED_PENALTY})
    # PCR capability from the inch range -- the only eligibility statement PCR has
    for r in uni.filter(pl.col("plant") == "PCR").iter_rows(named=True):
        rim = gt_rim.get(r["gt_code"])
        if rim is None:
            continue
        for n, (lo, hi) in PCR_INCH.items():
            if lo <= rim <= hi:
                m = f"TBMPCR{n}Stage2"
                k = ("PCR", r["gt_code"], m)
                if k not in seen:
                    seen.add(k)
                    rows.append({"plant": "PCR", "gt_code": r["gt_code"],
                                 "machine": m, "basis": "INCH",
                                 "penalty": INCH_PENALTY})
    cm = pl.DataFrame(rows).join(uni, on=["plant", "gt_code"], how="inner")

    # dedication penalty: a GT observed on exactly one machine gets a tier-8
    # cost on every OTHER machine -- priced, reported, never forbidden
    ded = (obs.group_by(["plant", "gt_code"])
           .agg(pl.col("machine").n_unique().alias("n_used"),
                pl.col("machine").first().alias("home")))
    cm = (cm.join(ded, on=["plant", "gt_code"], how="left")
          .with_columns(pl.col("n_used").fill_null(0))
          .with_columns(
              pl.when((pl.col("n_used") == 1) & (pl.col("machine") != pl.col("home")))
              .then(pl.col("penalty") + DEDICATION_PENALTY)
              .otherwise(pl.col("penalty")).alias("penalty"))
          .with_columns(
              pl.when((pl.col("n_used") == 1) & (pl.col("machine") != pl.col("home")))
              .then(pl.lit(True)).otherwise(pl.lit(False)).alias("breaks_dedication")))
    cm.write_parquet(D / f"cap_machine_{a.month}.parquet")

    print("\n  machine eligibility (union with provenance)")
    print(f"    {'plant':<6}{'GTs':>5}{'pairs':>8}{'BOTH':>7}{'CERT':>7}"
          f"{'OBS':>7}{'INCH':>7}{'machines/GT p50':>17}")
    for p in ["PCR", "TBR"]:
        s = cm.filter(pl.col("plant") == p)
        if not s.height:
            continue
        per = s.group_by("gt_code").agg(pl.len().alias("n"))
        c = {b: s.filter(pl.col("basis") == b).height
             for b in ["BOTH", "CERTIFIED", "OBSERVED", "INCH"]}
        print(f"    {p:<6}{s['gt_code'].n_unique():>5}{s.height:>8}"
              f"{c['BOTH']:>7}{c['CERTIFIED']:>7}{c['OBSERVED']:>7}{c['INCH']:>7}"
              f"{float(per['n'].median()):>17.0f}")
    nb = cm.filter(pl.col("breaks_dedication")).height
    vol = (cm.filter(~pl.col("breaks_dedication")).select(["plant", "gt_code"])
           .unique().height)
    print(f"    dedication-breaking options priced at {DEDICATION_PENALTY:,}: "
          f"{nb} pairs across {cm['gt_code'].n_unique() - 0} GTs "
          f"({vol} GTs have an unpenalised home)")

    # ---- B16 TT/TL groups ----------------------------------------------
    tt = pl.read_parquet(INP / "tt_tl.parquet").filter(pl.col("sku") != "")
    tmap = tt.select(["sku", "tt_tl"]).unique(subset=["sku"])
    tbr = dem.filter(pl.col("plant") == "TBR").join(tmap, on="sku", how="left")
    q = tbr.group_by("tt_tl").agg(pl.col("qty").sum().alias("q"))
    tot = float(q["q"].sum())
    ttq = float(q.filter(pl.col("tt_tl") == "TT")["q"].sum() or 0)
    n_tt = int(round(9 * ttq / tot)) if tot else 0

    # B16 group assignment must SATISFY ELIGIBILITY, not assume make alignment.
    # A make-aligned split (TT=MESNAC 4-9, TL=SAV 1-3) left 30 GTs with no
    # eligible machine in their own group -- and B16 forbids spilling across the
    # boundary. With 9 machines there are only C(9,6)=84 partitions, so search
    # them exhaustively and pick a feasible one; ties break on make coherence
    # then on lowest machine number, so the result is deterministic.
    from itertools import combinations
    elig_by_gt: dict[str, set[str]] = {}
    for r in cm.filter(pl.col("plant") == "TBR").iter_rows(named=True):
        elig_by_gt.setdefault(r["gt_code"], set()).add(r["machine"])
    tag_by_gt: dict[str, str] = {}
    for r in tbr.iter_rows(named=True):
        if r["tt_tl"]:
            tag_by_gt[r["gt_code"]] = r["tt_tl"]
    qty_by_gt: dict[str, float] = {
        r["gt_code"]: float(r["q"]) for r in
        tbr.group_by("gt_code").agg(pl.col("qty").sum().alias("q")).iter_rows(named=True)}

    def uncovered(tt_set: set[int]) -> tuple[int, float]:
        """GTs (and volume) with no eligible machine inside their own group."""
        ttm = {f"TBMTBR{n}Stage2" for n in tt_set}
        tlm = {f"TBMTBR{n}Stage2" for n in TBR_MAKE if n not in tt_set}
        n = 0
        v = 0.0
        for g, tag in tag_by_gt.items():
            want = ttm if tag == "TT" else tlm
            if not (elig_by_gt.get(g, set()) & want):
                n += 1
                v += qty_by_gt.get(g, 0.0)
        return n, v

    # ---- MACHINE-SIDE FEASIBILITY -- the other half of the same test ------
    #
    # THE DEFECT THIS FIXES. `uncovered()` above asks "does every GT have a
    # machine in its group?". It never asked the mirror question: "does every
    # MACHINE have a GT in its group?". A machine whose eligible GTs are all
    # tagged the OTHER way is dead for the entire horizon -- B16 forbids
    # spilling across the boundary, so it can never be given work -- and nothing
    # in the plan reported it.
    #
    # Measured before the fix, by month:
    #   May 2026  TBMTBR7 -> TL, 6 eligible GTs, ZERO of them TL  -> 0 tyres
    #             built, while the PLANT ran that same machine at 89.6 % for
    #             11,587 tyres. TBMTBR5 -> TT with 3 in-group of 27 -> 14.4 %.
    #   Aug 2026  TBMTBR8 -> TL, 17 eligible GTs, ZERO of them TL -> stranded.
    #   Jun/Jul   no machine stranded -- which is exactly why this survived
    #             every tuning pass: they all ran on July.
    # One of nine machines dead is 11 % of TBR building capacity.
    #
    # A bipartite assignment has TWO feasibility sides. Checking one is not
    # checking feasibility.
    def stranded(tt_set: set[int]) -> tuple[int, float, dict]:
        """Machines with no -- or too little -- eligible in-group demand.

        Returns (n_dead, deficit_tyres, reach_by_machine).
        `reach(m)` is the demand of GTs tagged with m's group that m may build.
        A machine is DEAD at reach 0. `deficit` additionally counts machines
        that cannot even claim an equal share of their own group's demand, so a
        machine that is technically alive but nearly idle still costs the
        partition something (TBMTBR5: 3 in-group GTs of 27).
        """
        grp_of = {n: ("TT" if n in tt_set else "TL") for n in TBR_MAKE}
        reach: dict[int, float] = {}
        for n in TBR_MAKE:
            mach = f"TBMTBR{n}Stage2"
            reach[n] = sum(
                qty_by_gt.get(g, 0.0) for g, tag in tag_by_gt.items()
                if tag == grp_of[n] and mach in elig_by_gt.get(g, set()))
        dead = sum(1 for n in TBR_MAKE if reach[n] <= 0.0)
        deficit = 0.0
        for tag in ("TT", "TL"):
            ms = [n for n in TBR_MAKE if grp_of[n] == tag]
            if not ms:
                continue
            share = sum(qty_by_gt.get(g, 0.0) for g, t in tag_by_gt.items()
                        if t == tag) / len(ms)
            deficit += sum(max(0.0, share - reach[n]) for n in ms)
        return dead, deficit, reach

    # Which criterion: "gt" = the old GT-side-only search (for A/B), "machine" =
    # + dead-machine count, "coverage" = + the fair-share deficit as a tiebreak.
    B16_CRIT = os.environ.get("PLANNER_B16_CRITERION", "coverage")

    best = None
    for combo in combinations(sorted(TBR_MAKE), n_tt):
        cnt, vol_bad = uncovered(set(combo))
        dead, deficit, _ = stranded(set(combo))
        makes = len({TBR_MAKE[n] for n in combo})
        # GT-side stays PRIMARY: an uncovered GT cannot be built at all, while a
        # stranded machine only wastes capacity. Never trade the first for the
        # second.
        if B16_CRIT == "gt":
            key = (cnt, vol_bad, makes, combo)
        elif B16_CRIT == "machine":
            key = (cnt, vol_bad, dead, makes, combo)
        else:
            key = (cnt, vol_bad, dead, deficit, makes, combo)
        if best is None or key < best[0]:
            best = (key, combo)
    tt_m = sorted(best[1])
    tl_m = [n for n in TBR_MAKE if n not in tt_m]
    bad, bad_vol = uncovered(set(tt_m))
    n_dead, deficit, reach = stranded(set(tt_m))

    grp = pl.DataFrame(
        [{"plant": "TBR", "machine": f"TBMTBR{n}Stage2", "make": TBR_MAKE[n],
          "group": ("TT" if n in tt_m else "TL")} for n in TBR_MAKE])
    grp.write_parquet(D / f"cap_ttl_groups_{a.month}.parquet")
    print(f"\n  B16 TT/TL groups   TT {100*ttq/tot:.1f}% of volume "
          f"-> {n_tt} TT / {9-n_tt} TL   (searched {len(list(combinations(range(9), n_tt)))} partitions)")
    print(f"    TT ({len(tt_m)}): {[f'TBM{n}' for n in tt_m]}  "
          f"makes {sorted({TBR_MAKE[n] for n in tt_m})}")
    print(f"    TL ({len(tl_m)}): {[f'TBM{n}' for n in tl_m]}  "
          f"makes {sorted({TBR_MAKE[n] for n in tl_m})}")
    if bad:
        print(f"    gate: {bad} GTs ({bad_vol:,.0f} tyres) have NO eligible machine "
              f"in their group  ***FAIL***")
        print(f"    -> B16 step 7: report INFEASIBLE and re-split; do NOT spill "
              f"across the boundary")
    else:
        print(f"    gate: every TT/TL GT has an eligible machine in its group  PASS")

    # ---- MACHINE-SIDE REPORT -- never silent again -----------------------
    # A machine that produced ZERO tyres went unreported for the entire life of
    # this project. That silence is the defect underneath the defect, so the
    # per-machine reach table is printed on EVERY run, pass or fail, and
    # persisted for regression.
    grp_of = {n: ("TT" if n in tt_m else "TL") for n in TBR_MAKE}
    print(f"    criterion: {B16_CRIT}   in-group eligible demand per machine "
          f"(reach = demand of same-tag GTs this machine may build)")
    mrows = []
    for n in TBR_MAKE:
        mach = f"TBMTBR{n}Stage2"
        el = sum(1 for g in elig_by_gt if mach in elig_by_gt[g])
        ing = sum(1 for g, t in tag_by_gt.items()
                  if t == grp_of[n] and mach in elig_by_gt.get(g, set()))
        flag = "  <<< STRANDED (0 in-group)" if reach[n] <= 0 else ""
        print(f"      {mach:<16}{grp_of[n]:<3} eligible {el:>3} | in-group "
              f"{ing:>3} | reach {reach[n]:>9,.0f}{flag}")
        mrows.append({"plant": "TBR", "machine": mach, "group": grp_of[n],
                      "eligible_gts": el, "in_group_gts": ing,
                      "reach_tyres": float(reach[n]),
                      "stranded": bool(reach[n] <= 0), "month": a.month})
    pl.DataFrame(mrows).write_parquet(D / f"b16_machine_reach_{a.month}.parquet")

    if n_dead:
        # FALLBACK, STATED. The search already MINIMISES dead machines, so if
        # any remain, NO partition of these 9 machines avoids it: the machine's
        # eligibility simply contains no GT of the group its load balance forces
        # it into. That is a data/eligibility fact, not a search failure, and the
        # honest response is to ship the best-effort split and SHOUT -- not to
        # break B16 by spilling, and not to rebalance n_tt, which would move the
        # strand rather than remove it.
        print(f"    !! {n_dead} MACHINE(S) STRANDED -- zero in-group eligible "
              f"demand for the whole horizon. This is ~{100*n_dead/9:.0f}% of TBR "
              f"building capacity that CANNOT be used.")
        print(f"    !! Best-effort split shipped (the search already minimises "
              f"this). Fix is upstream: widen that machine's eligibility, or "
              f"re-tag its GTs. Report to plant.")
    else:
        print(f"    gate: every machine has eligible in-group demand  PASS "
              f"(fair-share deficit {deficit:,.0f} tyres)")

    # ---- press eligibility + mould cap ---------------------------------
    # PRESSES ARE A ROSTER, NOT A CAPABILITY. The eligibility matrix lists every
    # press a GT could ever run on, including presses the plant did not operate
    # that month. Planning against 92 PCR presses when only 86 ran inflates
    # capacity and makes every comparison with the plant unfair.
    pm = pl.read_parquet(D / "allowed_press_matrix.parquet")
    cp = pm.join(uni, on=["plant", "gt_code"], how="inner")
    roster_f = ROOT / "masters" / f"press_list_{a.month}.json"
    if roster_f.exists():
        import json as _json
        roster = _json.loads(roster_f.read_text())
        before = {p: cp.filter(pl.col("plant") == p)["press"].n_unique()
                  for p in ["PCR", "TBR"]}
        keep = pl.DataFrame(
            [{"plant": p, "press": x} for p, v in roster.items() for x in v])
        cp = cp.join(keep, on=["plant", "press"], how="inner")
        print(f"\n  press roster (masters/press_list_{a.month}.json)")
        for p in ["PCR", "TBR"]:
            n = cp.filter(pl.col("plant") == p)["press"].n_unique()
            miss = len(set(roster[p]) - set(cp.filter(pl.col("plant") == p)["press"].to_list()))
            print(f"    {p}: eligible {before[p]} -> rostered {n} "
                  f"(roster {len(roster[p])}, {miss} rostered presses have no "
                  f"eligible GT -- matrix coverage gap, not a capacity gain)")
    else:
        print(f"\n  WARNING: no press_list_{a.month}.json -- planning against the "
              f"full eligibility matrix, which OVERSTATES capacity")
    cp.write_parquet(D / f"cap_press_{a.month}.parquet")

    mp = pl.read_csv(SRC / "curing_item_mould_mapping 2.csv", infer_schema_length=0)
    inv = pl.read_csv(SRC / "mould_inv_ctp_17072026.csv", infer_schema_length=0)
    act = set(inv.filter(pl.col("CurrStat") == "ACTIVE")["Equnr"].to_list())
    per = (mp.with_columns(pl.col("Mold_Name").is_in(list(act)).alias("a"))
           .group_by("Item_Code").agg(pl.col("a").sum().cast(pl.Int64).alias("m")))
    gm = (dem.select(["plant", "gt_code", "sku"]).unique()
          .join(per, left_on="sku", right_on="Item_Code", how="left")
          .group_by(["plant", "gt_code"])
          .agg(pl.col("m").sum().cast(pl.Int64).alias("moulds"))
          .with_columns(pl.col("moulds").fill_null(0)))
    # RECONCILE against observed concurrency. The master undercounts: GT 1482
    # UHL lists 2 moulds but the plant ran 6 presses on it concurrently and made
    # 20,660 tyres in July. Capping at 2 makes a month the plant demonstrably
    # completed look physically impossible (1,621 serial hours against 744).
    # The master is a FLOOR; a press observed running the GT proves a mould
    # existed for it. Same rule as L1's curability reconciliation.
    conc = duck().execute("""
        WITH c AS (SELECT b.plant, b.itemCode gt, CAST(cur.wcID AS VARCHAR) press,
                          cur.date
                   FROM v_curing cur JOIN v_build b ON cur.gtbarCode = b.productionID
                   WHERE b.stage = 2 AND b.itemCode IS NOT NULL AND cur.wcID IS NOT NULL),
             d AS (SELECT plant, gt, date, count(DISTINCT press) AS n
                   FROM c GROUP BY 1,2,3)
        SELECT plant, gt AS gt_code, max(n) AS observed_max
        FROM d GROUP BY 1,2""").pl()
    gm = (gm.join(conc, on=["plant", "gt_code"], how="left")
          .with_columns(pl.col("observed_max").fill_null(0).cast(pl.Int64))
          .with_columns(pl.max_horizontal("moulds", "observed_max").alias("moulds")))
    under = gm.filter(pl.col("observed_max") > pl.col("moulds").cast(pl.Int64))         if False else gm.filter(pl.col("observed_max") > 0)
    gm = gm.with_columns(
        pl.when(pl.col("moulds") > 0).then(pl.col("moulds"))
        .otherwise(None).alias("max_concurrent_presses"))
    gm.write_parquet(D / f"cap_mould_{a.month}.parquet")
    unknown = gm.filter(pl.col("moulds") == 0).height
    print(f"\n  mould cap (R3): moulds/GT p50 {float(gm['moulds'].median()):.0f}  "
          f"max {int(gm['moulds'].max())}   "
          f"{unknown} GTs unknown -> uncapped, flagged")

    # ---- changeover matrix ---------------------------------------------
    ch = []
    for n in range(1, 12):
        t = PCR_TYPE[n]
        ch.append({"plant": "PCR", "machine": f"TBMPCR{n}Stage2",
                   "machine_type": t, "same_min": CHG[t][0],
                   "diff_min": CHG[t][1], "crew": CHG[t][2],
                   "inch_lo": PCR_INCH[n][0], "inch_hi": PCR_INCH[n][1],
                   "inch_source": PCR_INCH_SRC[n]})
    for n in TBR_MAKE:
        ch.append({"plant": "TBR", "machine": f"TBMTBR{n}Stage2",
                   "machine_type": TBR_MAKE[n], "same_min": CHG["TBR"][0],
                   "diff_min": CHG["TBR"][1], "crew": CHG["TBR"][2],
                   "inch_lo": None, "inch_hi": None, "inch_source": None})
    cf = pl.DataFrame(ch)
    cf.write_parquet(D / "cap_changeover.parquet")
    print(f"\n  changeover matrix ({cf.height} machines)")
    for r in cf.group_by(["plant", "machine_type"]).agg(
            pl.len().alias("n"), pl.col("same_min").first(),
            pl.col("diff_min").first()).sort(["plant", "machine_type"]).iter_rows(named=True):
        print(f"    {r['plant']} {r['machine_type']:<7}{r['n']:>3} machines   "
              f"same {r['same_min']:>3} min   diff {r['diff_min']:>3} min")

    print(f"\n  -> cap_machine / cap_press / cap_mould / cap_ttl_groups / "
          f"cap_changeover in {D}")


if __name__ == "__main__":
    main()
