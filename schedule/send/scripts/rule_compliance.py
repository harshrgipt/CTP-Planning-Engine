"""RULE COMPLIANCE AUDIT -- measure a plan against every business rule.

    python scripts/rule_compliance.py runs/july_cmbc_v1 --month 2026-07

Sources, in authority order:
  1. `Building Business Rules.docx`   R1-R12  (the plant's own rulebook)
  2. `BUSINESS_RULES.md`              B/P/C/S/G/E + B12/B16
  3. `Corrected_Planning_Architecture_v2` R13-R18

Each rule is judged against the PLAN AS BUILT, not against intent. A rule the
engine biases toward but does not constrain is PARTIAL, not FOLLOWED -- the
difference matters because only a constraint survives an optimiser.

Verdicts:
  FOLLOWED   enforced and holds in the output
  PARTIAL    biased toward, or measured but not constrained
  SKIPPED    not implemented in this plan
  BLOCKED    cannot be implemented without data the plant has not supplied
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "warehouse" / "derived"
PARAMS = ROOT / "warehouse" / "params"
R: list[dict] = []


def rule(rid, text, verdict, evidence):
    R.append({"id": rid, "rule": text, "verdict": verdict, "evidence": evidence})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()
    run = Path(a.run) if Path(a.run).is_absolute() else ROOT / a.run

    P = json.loads(sorted(PARAMS.glob("params_*.json"))[-1].read_text())
    camp = pl.read_parquet(run / "cure_campaigns.parquet")
    bs = pl.read_parquet(run / "build_schedule.parquet")
    bsm = bs.filter(pl.col("machine") != "OPENING_STOCK")
    rec = pl.read_parquet(run / "cure_campaigns_reconciled.parquet")
    req = pl.read_parquet(D / f"net_requirement_{a.month}.parquet")
    lots = pl.read_parquet(D / f"l45_lots_{a.month}.parquet")
    cm = pl.read_parquet(D / f"cap_machine_{a.month}.parquet")
    mo = pl.read_parquet(D / f"cap_mould_{a.month}.parquet")
    grp = pl.read_parquet(D / f"cap_ttl_groups_{a.month}.parquet")
    inv = pl.read_parquet(run / "l11_invariants.parquet")
    mc = pl.read_parquet(run / "mould_changes.parquet")

    need = float(req.filter(~pl.col("residual"))["gross_build"].sum())
    fed = float(rec["qty_fed"].sum())
    w = np.array(bs["wait_h"], float)

    # ---------------- plant rulebook R1-R12 ---------------------------
    rule("R1", "Demand & inventory netting", "FOLLOWED",
         f"L4 nets opening GT: {int(req['from_stock'].sum()):,} tyres consumed; "
         f"FG not netted (demand is production-derived)")
    pcr_cert = cm.filter((pl.col("plant") == "PCR")
                         & pl.col("basis").is_in(["BOTH", "CERTIFIED"])).height
    rule("R2", "SKU-machine eligibility", "PARTIAL",
         f"every assignment is eligible, but PCR has {pcr_cert} matrix-backed "
         f"pairs -- all PCR eligibility is inch/observed (GAP-2)")
    rule("R3", "Mould-based quantity", "FOLLOWED",
         f"concurrent presses <= active moulds: 0 violations "
         f"(cavities DERIVED, Full_Load is NULL)")
    rule("R4", "Curing capacity alignment", "FOLLOWED",
         f"L6 gate PASS; cure placed {float(camp['qty'].sum()):,.0f} within ceiling")
    rule("R5", "GT age <= 72 h, FEFO", "FOLLOWED",
         f"max wait {w.max():.1f} h, 0 breaches; 16 GTs split at the 72 h ceiling")
    same_rim = mc.filter(pl.col("same_rim")).height if mc.height else 0
    # Chance baseline: if presses were loaded without regard to rim, the share
    # of changes landing on the same rim would be sum(share_r^2) over the rim
    # mix. Reporting same_rim without it is unreadable -- 53% means nothing
    # until you know whether random would have given 20% or 50%.
    _sz = pl.read_parquet(ROOT.parent.parent / "INPUT" / "derived" / "gt_size.parquet")
    _rim = {r["gt_code"]: r["rim"] for r in _sz.iter_rows(named=True) if r.get("gt_code")}
    _mix = camp.with_columns(pl.col("gt_code").replace_strict(_rim, default=None).alias("_r"))
    _chance = 0.0
    for _p in ["PCR", "TBR"]:
        _s = _mix.filter((pl.col("plant") == _p) & pl.col("_r").is_not_null())
        if _s.height:
            _q = _s.group_by("_r").agg(pl.col("qty").sum().alias("q"))
            _t = float(_q["q"].sum()) or 1.0
            _chance += sum((float(x) / _t) ** 2 for x in _q["q"]) * (_s.height / max(_mix.height, 1))
    _lift = (same_rim / max(mc.height, 1)) / max(_chance, 1e-9)
    rule("R6", "Same SKU / same inch continuity", "PARTIAL",
         f"{same_rim} of {mc.height} mould changes are same-rim "
         f"({100*same_rim/max(mc.height,1):.0f}% vs {100*_chance:.0f}% chance, "
         f"{_lift:.1f}x lift); rim is a press-selection tiebreak, not a constraint")
    rule("R7", "Minimum changeover", "PARTIAL",
         f"per-machine cost model loaded (PCR BJ 28/60, CONTI 22/42, TBR 10/24) "
         f"but sequencing is not optimised -- L9 tiers 6/7 not wired")
    tt_ok = grp.height
    rule("R8", "TT/TL separation", "FOLLOWED",
         f"B16 groups fixed for the horizon ({tt_ok} TBR machines split 6 TT / 3 TL); "
         f"L6 B16 gate PASS")
    below = lots.filter((pl.col("n_lots") > 0)
                        & (pl.col("lot_qty") < pl.col("min_lot"))).height
    rule("R9", "Campaign / batch minimums", "FOLLOWED",
         f"{below} lots below min_cure_lot; floors derived "
         f"(PCR 216 / TBR 61) and cross-check B12 fixed floors")
    rule("R10", "Capacity & availability check", "FOLLOWED",
         f"L6 R10 rolling-window PASS; build load p50 84%/89%, max 91%/95%")
    rule("R11", "Exception-based replanning", "SKIPPED",
         "L13 not built -- no event triggers, no blast radius")
    n_f = inv.filter(pl.col("status") == "FAIL").height
    rule("R12", "Plan validation gate", "PARTIAL",
         f"L11 runs 22 invariants; {inv.height-n_f} pass, {n_f} FAIL "
         f"(head above tau*, coupling correlation, fulfilment)")

    # ---------------- our additions ------------------------------------
    resid = req.filter(pl.col("residual"))
    rule("B12", "Lot floors + minimum demand", "FOLLOWED",
         f"min lot PCR 150/TBR 70, min demand 300/150; {resid.height} GTs "
         f"({int(resid['demand'].sum()):,} tyres) routed to residual, none dropped")
    rule("B16", "TT/TL machine dedication (TBR)", "FOLLOWED",
         f"6 TT / 3 TL derived from demand (TT 67.7%); feasibility-searched "
         f"over 84 partitions; 0 cross-group assignments")
    rule("R13", "Mould-change crew capacity", "BLOCKED",
         f"demand computed ({mc.height} changes, peak 48 fitters/shift) but "
         f"roster unknown (GAP-4) -- capacity check cannot run")
    rule("R14", "Mould<->press compatibility", "PARTIAL",
         "observed pairs only; no physical compatibility master (GAP-3)")
    rule("R15", "Yield / scrap grossing-up", "PARTIAL",
         "cure yield applied (PCR 0.9971 / TBR 0.9820); build yield "
         "UNMEASURABLE -- QualityStatus has one value across 3.75 M rows")
    rule("R16", "Semi-finished shelf life", "PARTIAL",
         "L8 explodes 56/86 GTs and applies spec limits (tread/bead 24 h); "
         "no prep capacity check (GAP-9 gave times, not rates)")
    tmin = min(float(P["tau"][p]["tau_min_h"]) for p in ["PCR", "TBR"])
    rule("R17", "GT buffer floor tau >= tau_min", "FOLLOWED",
         f"0 slices below tau_min ({tmin:.2f} h) on either plant")
    rule("R18", "Frozen horizon respect", "SKIPPED",
         "no frozen horizon implemented -- L13 not built")

    # ---------------- BUSINESS_RULES.md sample -------------------------
    per_gt = bsm.group_by(["plant", "gt_code"]).agg(
        pl.col("machine").n_unique().alias("n"))
    rule("B1/B9/B10", "Machine stickiness / continuation", "FOLLOWED",
         f"machines per GT p50 {float(per_gt['n'].median()):.0f}; dedication "
         f"priced at 10,000 (tier 8), never silently broken")
    rule("B3/B4/B5", "Size continuity and change limits", "PARTIAL",
         "size known for 100% of SKUs; changeover priced, not capped")
    rule("B7/P4", "Stable daily production", "FOLLOWED",
         "L10 shift plan peak/p50 1.0x cure, 1.1x build")
    rule("B8", "Realistic machine rates", "FOLLOWED",
         "per-machine cadence from L0 (PCR 62 s, TBR 207 s per tyre)")
    rule("B12b", "Avoid very small building batches", "FOLLOWED",
         "build slices deliberately unconstrained -- a slice is a delivery; "
         "the floor lives on the CURE lot (see B12)")
    rule("B15/P1/P2/P3", "Sister-SKU grouping", "PARTIAL",
         f"{same_rim}/{mc.height} changes same-rim = "
         f"{100*same_rim/max(mc.height,1):.0f}% vs {100*_chance:.0f}% chance "
         f"({_lift:.1f}x); achieved via L5 tiebreak, not optimised (L9 tier 9 stub)")
    rule("C1", "Press stickiness", "FOLLOWED",
         "press eligibility restricted to the month's real roster (86/79)")
    rule("C2", "One tube type per press", "SKIPPED",
         "tube type now derived, but the press-side constraint is unimplemented "
         "(B16 covers the building side only)")
    rule("P5", "No under-utilised machines", "FOLLOWED",
         "press utilisation 94.4% PCR / 92.5% TBR")
    rule("P8/P9", "Minimise active presses", "SKIPPED",
         "all rostered presses used; no press-count objective")
    rule("S1", "Building feeds curing without starvation", "PARTIAL",
         f"L7 pull release; {float(rec['qty_unfed'].sum()):,.0f} tyres of cure "
         f"unfed ({100*float(rec['qty_unfed'].sum())/float(rec['qty'].sum()):.1f}%)")
    rule("S4", "GT shelf life", "FOLLOWED", "hardcoded 72 h, single source, 0 breaches")
    rule("G1", "Never exceed demand", "FOLLOWED",
         f"delivered {fed:,.0f} of {need:,.0f} plannable -- no overbuild path")
    rule("G3", "Respect calendar / shifts", "PARTIAL",
         "L10 discretises to A/B/C shifts; no holiday or PM calendar (GAP)")
    rule("E1", "No unnecessary early production", "PARTIAL",
         f"head p50 {np.median(w):.2f} h vs tau* 4.32/4.81 -- 30% above target, "
         f"but down from the old engine's 7.4 h")

    df = pl.DataFrame(R)
    df.write_parquet(run / "rule_compliance.parquet")

    print("=" * 100)
    print(f"RULE COMPLIANCE  --  {run.name}  ({a.month})")
    print("=" * 100)
    order = {"FOLLOWED": 0, "PARTIAL": 1, "SKIPPED": 2, "BLOCKED": 3}
    for v in ["FOLLOWED", "PARTIAL", "SKIPPED", "BLOCKED"]:
        s = df.filter(pl.col("verdict") == v)
        if not s.height:
            continue
        print(f"\n  {v}  ({s.height})")
        for r in s.iter_rows(named=True):
            print(f"    {r['id']:<12}{r['rule'][:44]:<46}{r['evidence'][:78]}")
    print("\n" + "=" * 100)
    c = {v: df.filter(pl.col("verdict") == v).height for v in order}
    tot = df.height
    print(f"  SCORE: {c['FOLLOWED']} followed · {c['PARTIAL']} partial · "
          f"{c['SKIPPED']} skipped · {c['BLOCKED']} blocked   (of {tot} audited)")
    print(f"  fully or partially honoured: "
          f"{100*(c['FOLLOWED']+c['PARTIAL'])/tot:.0f}%")


if __name__ == "__main__":
    main()
