"""L2.5 -- CAMPAIGN INTELLIGENCE ENGINE.  Advisor, not generator.

    python -m planner.cmbc.l25_cie --month 2026-07

CIE PROPOSES. L3 ceiling and L6 gate DISPOSE.
  Nothing here is a decision. Every output is a candidate carrying a confidence
  and a provenance string, and any of them may be rejected downstream without
  the model being wrong. That separation is the point: an advisor that silently
  becomes a generator reintroduces exactly the policy-learning failure L0 avoids
  -- it would propose what the plant already does and call it optimal.

Two halves:

  A. PARAMETER ESTIMATION (safe -- description of history)
     * MOULD SETS: connected components of the GT<->mould graph. Two GTs that
       share a mould cannot cure simultaneously on separate presses, so the
       mould set, not the GT, is the real capacity unit. L0 measured lot sizes
       per GT; this is the level L4.5 actually needs.
     * observed campaign quantity distribution per mould set
     * split/merge triggers, read off the visit law rather than assumed

  B. PROPOSAL GENERATION (constrained)
     For each planned GT: candidate campaign shapes (n, qty, hours) with a
     confidence and the evidence behind them.

CONFIDENCE IS EVIDENCE COUNT, NOT MODEL FIT
  A shape backed by 40 historical campaigns of the same GT is high confidence.
  One backed by the plant-level band because the GT is new is low confidence and
  says so. Reporting a fitted R2 here would flatter the estimate -- the question
  is "how much have we seen this?", not "how well did a curve fit?".
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl
from planner import paths

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = paths.RAW
D = ROOT / "warehouse" / "derived"
INP = paths.INPUT_DERIVED
PARAMS = ROOT / "warehouse" / "params"


def mould_sets(dem: pl.DataFrame) -> pl.DataFrame:
    """Connected components of the GT <-> mould bipartite graph.

    Two GTs sharing a mould compete for it: they cannot cure at the same time on
    different presses. The component is therefore the capacity unit for L4.5,
    not the individual GT.
    """
    mp = pl.read_csv(paths.raw("curing_item_mould_mapping 2.csv"), infer_schema_length=0)
    gs = dem.select(["plant", "gt_code", "sku"]).unique()
    j = gs.join(mp.select(["Item_Code", "Mold_Name"]),
                left_on="sku", right_on="Item_Code", how="inner")

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)      # deterministic: lexical min

    for r in j.iter_rows(named=True):
        union(f"G::{r['plant']}::{r['gt_code']}", f"M::{r['Mold_Name']}")

    out = []
    for r in gs.iter_rows(named=True):
        key = f"G::{r['plant']}::{r['gt_code']}"
        out.append({"plant": r["plant"], "gt_code": r["gt_code"],
                    "mould_set": find(key) if key in parent else key})
    return pl.DataFrame(out).unique(subset=["plant", "gt_code"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-07")
    a = ap.parse_args()

    dem = pl.read_parquet(ROOT / "masters" / "demand" / f"demand_{a.month}.parquet")
    pj = sorted(PARAMS.glob("params_*.json"))
    if not pj:
        raise SystemExit("no L0 parameter file -- run l0_learn first")
    P = json.loads(pj[-1].read_text())
    lots = pl.read_parquet(PARAMS / P["tables"]["observed_lot_sizes"])
    from planner.config import CONFIG
    min_lot = CONFIG.thresholds.min_lot_units
    min_dem = CONFIG.thresholds.min_demand_units

    print("=" * 92)
    print(f"L2.5  CAMPAIGN INTELLIGENCE ENGINE  --  {a.month}   (advisor)")
    print("=" * 92)
    print(f"  L0 parameters: {pj[-1].name}\n")

    # ---- A. mould sets --------------------------------------------------
    ms = mould_sets(dem)
    g = (dem.group_by(["plant", "gt_code"]).agg(pl.col("qty").sum().alias("demand"))
         .join(ms, on=["plant", "gt_code"], how="left"))
    g = g.with_columns(pl.col("mould_set").fill_null(
        pl.concat_str([pl.lit("SOLO::"), pl.col("plant"), pl.lit("::"),
                       pl.col("gt_code")])))
    comp = (g.group_by(["plant", "mould_set"])
            .agg(pl.col("gt_code").n_unique().alias("gts"),
                 pl.col("demand").sum().alias("demand")))
    print("  A. MOULD SETS (connected components of GT<->mould)")
    print(f"    {'plant':<6}{'sets':>6}{'GTs':>6}{'sets with >1 GT':>18}"
          f"{'largest set':>13}{'demand in shared sets':>24}")
    for p in ["PCR", "TBR"]:
        s = comp.filter(pl.col("plant") == p)
        sh = s.filter(pl.col("gts") > 1)
        tot = float(s["demand"].sum())
        print(f"    {p:<6}{s.height:>6}{int(s['gts'].sum()):>6}{sh.height:>18}"
              f"{int(s['gts'].max()):>13}"
              f"{100*float(sh['demand'].sum())/max(tot,1):>23.1f}%")
    print("    -> GTs in a shared set COMPETE for the same moulds; they cannot")
    print("       cure concurrently on separate presses. This is the L4.5 unit.")

    # ---- A. split/merge triggers, from the visit law --------------------
    print("\n  A. SPLIT/MERGE TRIGGER (read off the visit law, not assumed)")
    for p in ["PCR", "TBR"]:
        v = P["visit_law"].get(p)
        if not v:
            continue
        print(f"    {p}: n_campaigns = exp({v['intercept_a']}) * Q^{v['exponent_b']}  "
              f"CV(b) {v['cv_b']}  R2 {v['r2_min']}-{v['r2_max']} over "
              f"{v['n_months']} months")

    # ---- B. proposals ---------------------------------------------------
    hist = lots.rename({"gt": "gt_code"}) if "gt" in lots.columns else lots
    prop = []
    for r in g.iter_rows(named=True):
        p, gt, dq = r["plant"], r["gt_code"], float(r["demand"])
        if dq < min_dem[p]:
            prop.append({"plant": p, "gt_code": gt, "demand": dq,
                         "n_campaigns": 0, "qty_per": 0.0, "hours_per": 0.0,
                         "confidence": "n/a", "evidence": 0,
                         "provenance": f"below min_demand {min_dem[p]} -> residual policy"})
            continue
        v = P["visit_law"].get(p, {})
        b = float(v.get("exponent_b", 0.5))
        band = P["campaign_bands"].get(p, {}).get("cure", {})
        h50 = float(band.get("hours_p50", 48.0))
        # intercept comes from L0, never hardcoded -- a magic constant here
        # silently decouples the proposal from the learned law.
        a_i = v.get("intercept_a")
        if a_i is None:
            raise SystemExit(f"L0 params carry no intercept_a for {p} -- rerun l0_learn")
        n_law = max(1.0, np.exp(float(a_i)) * dq ** b)
        n_floor = dq / max(min_lot[p], 1)           # B12 floor on lot size
        n = int(max(1, min(round(n_law), np.floor(n_floor) or 1)))
        h = hist.filter((pl.col("plant") == p) & (pl.col("gt_code") == gt))
        ev = int(h["n_runs"][0]) if h.height else 0
        conf = ("high" if ev >= 20 else "medium" if ev >= 5
                else "low" if ev else "none")
        src = (f"GT history n={ev}" if ev else f"{p} plant band")
        prop.append({"plant": p, "gt_code": gt, "demand": dq,
                     "n_campaigns": n, "qty_per": round(dq / n, 1),
                     "hours_per": round(h50, 1),
                     "confidence": conf, "evidence": ev,
                     "provenance": f"visit law a={a_i} b={b}; {src}; "
                                   f"lot floor {min_lot[p]}"})
    pr = pl.DataFrame(prop)
    pr.write_parquet(D / f"cie_proposals_{a.month}.parquet")
    comp.write_parquet(D / f"cie_mould_sets_{a.month}.parquet")

    print("\n  B. CAMPAIGN SHAPE PROPOSALS")
    act = pr.filter(pl.col("n_campaigns") > 0)
    print(f"    {'plant':<6}{'GTs':>5}{'campaigns':>11}{'qty/camp p50':>14}"
          f"{'high':>6}{'med':>5}{'low':>5}{'none':>6}")
    for p in ["PCR", "TBR"]:
        s = act.filter(pl.col("plant") == p)
        if not s.height:
            continue
        c = {k: s.filter(pl.col("confidence") == k).height
             for k in ["high", "medium", "low", "none"]}
        print(f"    {p:<6}{s.height:>5}{int(s['n_campaigns'].sum()):>11}"
              f"{float(s['qty_per'].median()):>14,.0f}"
              f"{c['high']:>6}{c['medium']:>5}{c['low']:>5}{c['none']:>6}")
    skipped = pr.filter(pl.col("n_campaigns") == 0)
    print(f"    below min_demand -> residual policy: {skipped.height} GTs, "
          f"{int(skipped['demand'].sum()):,} tyres")

    # sanity: does the proposal respect the lot floor?
    viol = act.filter(pl.col("qty_per") < pl.col("plant").replace_strict(min_lot))
    print(f"\n    lot-floor check: {viol.height} proposals below min_lot "
          f"{'PASS' if viol.height == 0 else 'FAIL'}")
    print(f"\n  -> cie_proposals_{a.month}.parquet · cie_mould_sets_{a.month}.parquet")
    print("  CIE has proposed. L3 (ceiling) and L6 (feasibility) dispose.")


if __name__ == "__main__":
    main()
