"""STATIC GT -> MACHINE PARTITION, built the way the plant builds it.

    PYTHONPATH=. python scripts/build_gt_machine_partition.py [2026-07]
    -> INPUT/derived/gt_machine_partition.parquet

WHY A PARTITION AND NOT A PIN
  Measured: the plant keeps 66.7 % of its PCR GTs on exactly ONE machine for a
  whole month and still holds 91.5 % same-size changeovers. We reach 27.5 % and
  86.9 %. Pinning our dynamic assignment harder was tried twice and made the
  weighted setup WORSE (242 -> 264/289 h), because a rigid bad partition is worse
  than a fluid one. The plant's machine->size map IS a partition: each machine
  owns a size, the GTs of a size are split across that size's machines ONCE, and
  nothing moves afterwards. This script builds that object.

THE BOOKING ORDER (the plant's own, from the 8-month matrix)
  TIER 1  PURE machines -- one size at >=95 % of 8-month volume. Booked FIRST and
          filled with their own size's GTs before anything else is placed.
              PCR  10->R13  7->R13  1->R17  3->R15  9->R18
              TBR  1,2,3,7,8 -> R20
  TIER 2  MULTI-SIZE machines -- 2-3 sizes at >=5 %. They take their historical
          size set, primary size first.
              PCR  4->R12  6->R14  11->R16  5->R13,R14  8->R15,R17
              TBR  4->R22.5,R20  5->R22.5  6->R22.5  9->R20,R22.5
  TIER 3  FLEX -- TBMPCR2 runs 5 sizes (R18 61 %, R17 13 %, R14 11 %). It is the
          plant's own tail absorber; it takes whatever tiers 1-2 could not seat.
  TIER 4  Anything still unseated goes to the machine with the most free hours,
          and THAT MACHINE IS THEN LOCKED TO THAT SIZE for the rest of the build
          -- "run that size only", so an overflow never costs a size change.

GTs are seated LARGEST-FIRST (LPT) and WHOLE. A GT is split across two machines
only when no single machine has room for it -- measured, that is 1 GT in R13
(the group packs to 856 h against a 707 h machine) and 1 in R12 (735 h of demand
against a single 707 h machine). Every other rim group fits one-GT-one-machine.

COLUMNS  plant · gt_code · rim · machine · hours · qty · tier · is_split
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

import numpy as np
import polars as pl

from planner.data.warehouse import duck
from planner import paths

ROOT = Path(__file__).resolve().parent.parent
DER = paths.INPUT_DERIVED
OUT = DER / "gt_machine_partition.parquet"
MONTH = sys.argv[1] if len(sys.argv) > 1 else "2026-07"

NDAYS = calendar.monthrange(int(MONTH[:4]), int(MONTH[5:7]))[1]
UTIL = float(__import__("os").environ.get("PLANNER_PART_UTIL", "0.95"))
# ^ share of the month's REAL hours (NDAYS x 24), matching MACH_UTIL_CAP in L7
PURE = 0.95                     # >= this 8-month share => tier 1
REAL = 0.05                     # >= this share => the machine "really runs" it
CAD_DEFAULT = {"PCR": 58.0, "TBR": 204.0}   # s/tyre fallback, mined (B8)
DEFAULT_DIFF = {"PCR": 42.0, "TBR": 24.0}   # different-size min fallback (G4)
# Split a GT across same-size machines above this many hours. 0 = never split.
# Tuned to land machines/GT on the plant's 1.40, not below it.
SPLIT_H = float(__import__("os").environ.get("PLANNER_PART_SPLIT_H", "250"))

# ---- TIER 0: SEED FROM THE PLANT'S OWN ASSIGNMENT --------------------------
# `PLANNER_PART_SEED=wb` seats each GT on the `Assigned_Machine` named in the
# SKU_Construction_Clusters workbooks (via `INPUT/derived/sku_con_cluster.parquet`)
# BEFORE tiers 1-5 run, but only where that machine already runs the GT's rim
# and still has free hours. Everything else falls through to the existing
# machinery unchanged.
#
# WHY A PREFERENCE AND NOT THE PARTITION ITSELF. The workbook assignment is an
# OBSERVATION of Feb-Jul behaviour, not a plan for one month's demand, and it is
# CAPACITY-INFEASIBLE as it stands:
#     July  TBR  TBMTBR4 140.2 %  TBMTBR1 123.4 %  TBMTBR2 114.4 %
#     Aug   TBR  TBMTBR4 142.0 %  TBMTBR7 119.1 %  TBMTBR8 112.3 %
#     both  PCR  TBMPCR7 gets ZERO load, and 31-35 % of PCR demand has no
#                workbook row at all (`GT 1513 XPC1 MSIL` alone is 16.7 %)
# Used raw it would strand hundreds of hours. Used as a tier-0 preference the
# free-hours test is still the existing one, so the result stays feasible and
# the G4 repair pass still runs. It is also ~93 % (PCR) / ~84 % (TBR) identical
# to `gt_home_machine`, i.e. close to the `HARD_PIN` "pin to home" arm that was
# measured net-negative twice -- which is exactly why it is a flag, not a
# default.
SEED_WB = __import__("os").environ.get("PLANNER_PART_SEED", "").strip().lower() == "wb"


def _load_wb_assignment() -> dict:
    """(plant, gt_code) -> workbook Assigned_Machine, mapped to the MES name."""
    f = DER / "sku_con_cluster.parquet"
    if not f.exists():
        print(f"  !! PLANNER_PART_SEED=wb but {f.name} is missing -- seeding is INERT."
              f"\n     Build it: python scripts/build_sku_con_cluster.py")
        return {}
    df = pl.read_parquet(f)
    out = {(r["plant"], r["gt_code"]): r["assigned_machine"]
           for r in df.iter_rows(named=True) if r.get("assigned_machine")}
    print(f"  TIER 0 SEED: {len(out)} (plant, GT) assignments from {f.name}")
    return out


def main() -> None:
    sz = pl.read_parquet(DER / "gt_size.parquet")
    rim = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
           if r.get("gt_code") and r.get("rim")}

    # ---- 8-month machine x size matrix ---------------------------------
    q = """SELECT plant, machineCode m, itemCode g, count(*) n FROM v_build
           WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
           GROUP BY 1, 2, 3"""
    h = pl.DataFrame(duck().execute(q).fetchall(),
                     schema=["plant", "m", "g", "n"], orient="row")
    h = h.with_columns(pl.col("g").map_elements(
        lambda x: rim.get(x, "?"), return_dtype=pl.Utf8).alias("rim"))
    mr = h.group_by(["plant", "m", "rim"]).agg(pl.col("n").sum().alias("n"))
    tot = mr.group_by(["plant", "m"]).agg(pl.col("n").sum().alias("t"))
    mr = (mr.join(tot, on=["plant", "m"])
          .with_columns((pl.col("n") / pl.col("t")).alias("sh"))
          .filter(pl.col("rim") != "?")
          .sort(["plant", "m", "sh"], descending=[False, False, True]))

    # machine -> ordered list of sizes it really runs, and its tier
    sizes: dict = {}
    for r in mr.filter(pl.col("sh") >= REAL).iter_rows(named=True):
        sizes.setdefault((r["plant"], r["m"]), []).append((r["rim"], r["sh"]))
    tier: dict = {}
    for k, v in sizes.items():
        tier[k] = 1 if (len(v) == 1 or v[0][1] >= PURE) else (
            2 if len(v) <= 3 else 3)

    # ---- PER-MACHINE CHANGEOVER COST (G4) ------------------------------
    # A size change does NOT cost the same everywhere. `v_changeover_build`:
    #     PCR machines 1-5    28 same / 60 different
    #     PCR machines 6-11   22 same / 42 different
    #     TBR all             10 same / 24 different
    # So when a machine has to carry a second size, put that on a CHEAP machine.
    # Measured before this fix: all 21 of our PCR different-size changeovers
    # landed on TBMPCR2 at 60 min -- a cost-weighted 60.0 min/changeover against
    # the plant's 55.7, because the plant puts 15 of its 63 on 42-min machines
    # and we put none. TBMPCR2 is the historical 5-size flex machine, but it is
    # also the most expensive one to change size on.
    # Read from the master, never hardcoded, so this generalises to TBR (where
    # the rates happen to be uniform, making it a no-op) and to any month.
    co: dict = {}
    try:
        for r in duck().execute(
                "SELECT machine, same_size_min, diff_size_min "
                "FROM v_changeover_build").fetchall():
            co[r[0]] = (float(r[1]), float(r[2]))
    except Exception as e:                     # master absent -> cost-blind
        print(f"  !! v_changeover_build unavailable ({e}); size-change cost "
              f"preference is OFF and machines are chosen on history alone")

    def diff_min(P: str, m: str) -> float:
        """Cost of ONE different-size changeover on this machine."""
        return co.get(m, (0.0, DEFAULT_DIFF.get(P, 42.0)))[1]

    # ---- PER-MACHINE CADENCE (B8) --------------------------------------
    # Mined s/tyre ranges 49-66 on PCR. A flat plant mean misallocates a
    # machine's capacity by up to 15 %, which on a partition that packs to 95 %
    # is the difference between seating a GT and stranding it.
    cad: dict = dict(CAD_DEFAULT)
    _cf = DER / "cycle_time_building.parquet"
    if _cf.exists():
        for r in pl.read_parquet(_cf).iter_rows(named=True):
            if r.get("s_per_tyre"):
                cad[r["machine"]] = float(r["s_per_tyre"])

    def hours_on(qty: float, plant: str, m: str) -> float:
        return qty * cad.get(m, CAD_DEFAULT[plant]) / 3600.0

    # ---- demand for THIS month, in tyres --------------------------------
    _lf = ROOT / "warehouse" / "derived" / f"l45_lots_{MONTH}.parquet"
    if not _lf.exists():
        raise SystemExit(
            f"no demand for {MONTH}: {_lf} missing. Run l4/l45 for that month "
            f"first -- the partition is sized against a SPECIFIC month's demand "
            f"and must be rebuilt whenever the demand file changes.")
    lots = pl.read_parquet(_lf).filter(pl.col("n_lots") > 0)
    gts: list = []
    for r in lots.iter_rows(named=True):
        s = r["lot_sizes"]
        n = (float(sum(s)) if s is not None and len(s)
             else float(r["lot_qty"]) * int(r["n_lots"]))
        gts.append({"plant": r["plant"], "gt_code": r["gt_code"],
                    "rim": rim.get(r["gt_code"], "?"), "qty": n,
                    # nominal hours, for ordering only; the real cost is
                    # per-machine and is recomputed at seat time
                    "hours": n * CAD_DEFAULT[r["plant"]] / 3600.0})
    _norim = [g["gt_code"] for g in gts if g["rim"] == "?"]
    if _norim:
        print(f"  !! {len(_norim)} GT(s) have no rim in gt_size and CANNOT be "
              f"partitioned; L7 falls back to its dynamic assignment for them: "
              f"{_norim[:5]}")

    rows, unseated = [], []
    print("=" * 96)
    print(f"STATIC GT -> MACHINE PARTITION   {MONTH}"
          f"{'   [TIER-0 SEED = workbook Assigned_Machine]' if SEED_WB else ''}")
    print("=" * 96)
    WBA = _load_wb_assignment() if SEED_WB else {}

    for P in ("PCR", "TBR"):
        # HOURS COME FROM THE CALENDAR. A hardcoded 744 over-allocates a
        # 28-day month by 10.7 % and a 30-day month by 3.3 %, which on a
        # partition packed to 95 % strands GTs that L7 then has to rescue.
        cap = NDAYS * 24.0 * UTIL
        mach = sorted({k[1] for k in sizes if k[0] == P})
        free = {m: cap for m in mach}
        locked: dict = {}                 # machine -> the size it is locked to
        t1 = [m for m in mach if tier[(P, m)] == 1]
        # Cheapest-to-change-size first WITHIN each tier: the tier says
        # whether a machine may take extra sizes, the cost says which of
        # them should.
        t2 = sorted([m for m in mach if tier[(P, m)] == 2],
                    key=lambda m: (diff_min(P, m), m))
        t3 = sorted([m for m in mach if tier[(P, m)] == 3],
                    key=lambda m: (diff_min(P, m), m))
        # SPLIT THE BIG GTs ON PURPOSE -- the plant does, and we must not be
        # stricter than it. A whole-GT partition seats 97.7 % of PCR GTs on one
        # machine (1.02 machines/GT) against the plant's 66.7 % / 1.40. That
        # purity is bought with TIME: a 400 h GT alone on a 707 h machine has to
        # deliver against every cure deadline from that one machine, so runs
        # split in time instead and exhaust the B12 sub-floor budget -- measured
        # as PCR `would breach min_lot` 7,153 -> 14,009 tyres, the whole of the
        # 2.8 pt fulfilment cost. Splitting a big GT across two SAME-SIZE
        # machines costs no size change at all, because both machines still run
        # only that size.
        pending = []
        for g in sorted([g for g in gts if g["plant"] == P],
                        key=lambda g: -g["hours"]):
            n = min(int(np.ceil(g["hours"] / SPLIT_H)), 3) if SPLIT_H > 0 else 1
            if n <= 1:
                pending.append(g)
                continue
            for i in range(n):
                pending.append({**g, "hours": g["hours"] / n,
                                "qty": g["qty"] / n, "_part": i})
        pending.sort(key=lambda g: -g["hours"])

        print(f"\n  {P}   cap {cap:.0f} h/machine   "
              f"tier1 {len(t1)} pure · tier2 {len(t2)} multi · tier3 {len(t3)} flex")

        def seat(g: dict, m: str, hrs: float, tg: int, split: bool) -> None:
            free[m] -= hrs
            locked.setdefault(m, g["rim"])
            rows.append({"plant": P, "gt_code": g["gt_code"], "rim": g["rim"],
                         "machine": m, "hours": round(hrs, 2),
                         "qty": round(g["qty"] * hrs / g["hours"], 1),
                         "tier": tg, "is_split": split})

        def owners(g: dict, pool: list) -> list:
            """Machines in `pool` that run this size, best share first, and that
            are not already locked to a DIFFERENT size."""
            out = []
            for m in pool:
                if locked.get(m, g["rim"]) != g["rim"]:
                    continue
                for i, (rr, sh) in enumerate(sizes[(P, m)]):
                    if rr == g["rim"]:
                        # i == 0 -> this is the machine's PRIMARY size, no size
                        # change implied. i > 0 -> the machine is taking a
                        # SECOND size and will pay diff_min every time it
                        # switches, so rank those by cost, cheapest first.
                        out.append((i, diff_min(P, m) if i else 0.0,
                                    -sh, -free[m], m))
                        break
            return [m for *_, m in sorted(out)]

        # TIER 0 -- the plant's own assignment, where it is legal and it fits.
        # Guarded three ways: the machine must already run this rim (otherwise
        # the seed would MANUFACTURE a size change, the opposite of the point),
        # it must have the hours, and the GT must not already sit there.
        if SEED_WB and WBA:
            still, n0, h0 = [], 0, 0.0
            for g in pending:
                m = WBA.get((P, g["gt_code"]))
                _h = hours_on(g["qty"], P, m) if m in free else 0.0
                if (m in free
                        and locked.get(m, g["rim"]) == g["rim"]
                        and any(rr == g["rim"] for rr, _ in sizes[(P, m)])
                        and free[m] >= _h - 1e-9
                        and not any(r["gt_code"] == g["gt_code"] and r["machine"] == m
                                    and r["plant"] == P for r in rows)):
                    seat(g, m, _h, 0, g.get("_part") is not None)
                    n0 += 1
                    h0 += _h
                else:
                    still.append(g)
            print(f"    tier 0 seed: {n0} of {len(pending)} GT-parts seated on the "
                  f"workbook's own machine ({h0:,.0f} h); {len(still)} fall through")
            pending = still

        # TIER 1 then 2 then 3: fill the purest machines first, whole GTs, LPT.
        for pool, tg in ((t1, 1), (t2, 2), (t3, 3)):
            still = []
            for g in pending:
                for m in owners(g, pool):
                    _h = hours_on(g["qty"], P, m)
                    if free[m] >= _h - 1e-9 and not any(
                            r["gt_code"] == g["gt_code"] and r["machine"] == m
                            and r["plant"] == P for r in rows):
                        seat(g, m, _h, tg, g.get("_part") is not None)
                        break
                else:
                    still.append(g)
            pending = still

        # TIER 4 -- overflow. Most free hours wins, and the machine is then
        # LOCKED TO THAT SIZE so the overflow never costs a size change.
        still = []
        for g in pending:
            cands = [m for m in mach
                     if locked.get(m, g["rim"]) == g["rim"]
                     and free[m] >= hours_on(g["qty"], P, m) - 1e-9]
            if cands:
                # Cheapest machine to change size on wins; free hours only break
                # ties. An overflow lands on a machine that then carries a size
                # it did not have, so it is exactly the case G4 prices.
                _m = min(cands, key=lambda m: (diff_min(P, m), -free[m], m))
                seat(g, _m, hours_on(g["qty"], P, _m), 4, False)
            else:
                still.append(g)
        pending = still

        # LAST RESORT -- the GT genuinely does not fit on any single machine.
        # Split it across same-size machines by remaining room, largest first.
        for g in pending:
            left = g["hours"]
            cands = sorted([m for m in mach if locked.get(m, g["rim"]) == g["rim"]
                            or any(rr == g["rim"] for rr, _ in sizes[(P, m)])],
                           key=lambda m: -free[m])
            for m in cands:
                if left <= 1e-9:
                    break
                take = min(max(free[m], 0.0), left)
                if take <= 1e-9:
                    continue
                seat(g, m, take, 5, True)
                left -= take
            if left > 1e-9:
                unseated.append({**g, "left_h": round(left, 1)})

        # ---- REPAIR PASS: MOVE SIZE CHANGES TO WHERE THEY COST LEAST (G4) ---
        # Seating is greedy and tier-ordered, so a rim can end up on a machine
        # that does not own it -- and if that machine is an expensive one, every
        # switch there costs the top rate all month. Measured before this pass:
        # TBMPCR2 (60 min, the dearest PCR machine) carried 395 h of R16 + R17
        # on top of its own R18, and took ALL 21 of our different-size
        # changeovers, while 826 h sat free on 42-min machines -- TBMPCR11 alone
        # had 306 h free and R16 is ITS primary rim.
        #
        # For every machine holding a rim that is not its primary, try to move
        # that load. Preference order:
        #   1. a machine ALREADY LOCKED TO THAT RIM -- the size change disappears
        #      entirely rather than merely getting cheaper
        #   2. failing that, a machine where a size change is cheaper
        # A move is only taken if it strictly reduces cost, so the pass can
        # never make the plan worse.
        moved = 0
        for _ in range(3):                       # a move can enable another
            prim: dict = {}
            for m in mach:
                mine = [r for r in rows if r["plant"] == P and r["machine"] == m]
                if mine:
                    byrim: dict = {}
                    for r in mine:
                        byrim[r["rim"]] = byrim.get(r["rim"], 0.0) + r["hours"]
                    prim[m] = max(byrim, key=byrim.get)
            for r in list(rows):
                if r["plant"] != P or prim.get(r["machine"]) == r["rim"]:
                    continue                      # already on its own machine
                here = diff_min(P, r["machine"])
                best, best_key = None, None
                for m2 in mach:
                    if m2 == r["machine"] or not any(
                            rr == r["rim"] for rr, _ in sizes[(P, m2)]):
                        continue
                    if any(x["plant"] == P and x["machine"] == m2
                           and x["gt_code"] == r["gt_code"] for x in rows):
                        continue                  # would re-split onto itself
                    need = hours_on(r["qty"], P, m2)
                    if free[m2] < need - 1e-9:
                        continue
                    # rank: native rim first (cost 0), then cheaper machines
                    native = 0 if prim.get(m2, r["rim"]) == r["rim"] else 1
                    key = (native, diff_min(P, m2), -free[m2], m2)
                    if native == 0 or diff_min(P, m2) < here:
                        if best_key is None or key < best_key:
                            best, best_key = m2, key
                if best is None:
                    continue
                free[r["machine"]] += r["hours"]
                r["hours"] = round(hours_on(r["qty"], P, best), 2)
                r["machine"] = best
                free[best] -= r["hours"]
                moved += 1
        if moved:
            print(f"    repair: moved {moved} (GT, machine) pair(s) to a cheaper "
                  f"or native-size machine")

        d = pl.DataFrame([r for r in rows if r["plant"] == P])
        npg = d.group_by("gt_code").agg(pl.col("machine").n_unique().alias("k"))
        k = np.array(npg["k"], float)
        print(f"    {d['gt_code'].n_unique()} GTs -> {d.height} (GT, machine) pairs"
              f" · {100*(k==1).mean():.1f}% on exactly ONE machine"
              f" · {k.mean():.2f} machines/GT")
        for tg, lbl in ((1, "pure"), (2, "multi"), (3, "flex"), (4, "overflow"),
                        (5, "split")):
            s = d.filter(pl.col("tier") == tg)
            if s.height:
                print(f"      tier {tg} {lbl:<9} {s.height:>3} pairs "
                      f"{float(s['hours'].sum()):>8,.0f} h")
        print(f"    machine load: min {min(cap-f for f in free.values()):.0f} h"
              f" · max {max(cap-f for f in free.values()):.0f} h of {cap:.0f} h")
        # size purity of the resulting partition
        bad = sum(1 for m in mach
                  if len({r["rim"] for r in rows
                          if r["plant"] == P and r["machine"] == m}) > 1)
        print(f"    machines carrying MORE THAN ONE size: {bad}/{len(mach)}")

    out = pl.DataFrame(rows).with_columns(pl.lit(MONTH).alias("month"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.write_parquet(OUT)
    if unseated:
        print(f"\n  !! UNSEATED: {len(unseated)} GT(s) "
              f"{sum(u['left_h'] for u in unseated):,.0f} h -- capacity short")
        for u in unseated[:6]:
            print(f"     {u['plant']} {u['gt_code']} rim {u['rim']} "
                  f"{u['left_h']:,.0f} h unseated")
    else:
        print("\n  every GT seated -- partition is capacity-feasible")
    print(f"\n  -> {OUT}")


if __name__ == "__main__":
    main()
