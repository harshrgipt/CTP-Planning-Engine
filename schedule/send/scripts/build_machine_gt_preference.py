"""Build an auditable month-specific GT/SKU -> machine preference table.

Eligibility always comes from the approved monthly allowable matrix.  The
eight-month MES home-machine table supplies preference only; it can never add
an unapproved machine.  For TBR the script also recomputes the B16 TT/TL
dedication, keeping GT coverage and machine reach ahead of historical affinity.

    python scripts/build_machine_gt_preference.py --month 2026-07 --activate
"""
from __future__ import annotations

import argparse
import shutil
import sys
from itertools import combinations
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from planner import paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--activate", action="store_true",
                    help="replace this month's B16 group file with the recommendation")
    a = ap.parse_args()

    month = a.month
    demand = pl.read_parquet(paths.demand(month))
    allowable = (pl.read_parquet(paths.MASTERS / f"allowable_{month}.parquet")
                 .select(["plant", "gt_code", "machine"]).unique())
    home = pl.read_parquet(paths.input_derived("gt_home_machine.parquet"))
    tags = (pl.read_parquet(paths.input_derived("tt_tl.parquet"))
            .filter(pl.col("sku") != "")
            .select(["sku", "tt_tl"]).unique(subset=["sku"]))

    gt = (demand.join(tags, on="sku", how="left")
          .group_by(["plant", "gt_code"])
          .agg(pl.col("qty").sum().alias("demand_qty"),
               pl.col("sku").unique().sort().alias("sku_codes"),
               pl.col("tt_tl").drop_nulls().first().alias("tt_tl")))
    pref = (allowable.join(gt, on=["plant", "gt_code"], how="inner")
            .join(home, on=["plant", "gt_code", "machine"], how="left")
            .with_columns(
                pl.col("rank").fill_null(999).cast(pl.Int32).alias("history_rank"),
                pl.col("share").fill_null(0.0).alias("history_share"),
                pl.col("tyres_8mo").fill_null(0).alias("history_tyres_8mo"),
                (pl.col("rank").fill_null(999) == 1).alias("historical_home"))
            .drop(["rank", "share", "tyres_8mo", "n_machines"])
            .sort(["plant", "gt_code", "history_rank", "history_share", "machine"],
                  descending=[False, False, False, True, False]))

    # Recompute B16 on approved pairs.  The primary terms are hard feasibility;
    # history breaks only feasible/load-equivalent ties.
    tbr = pref.filter(pl.col("plant") == "TBR")
    qty = {r["gt_code"]: float(r["demand_qty"])
           for r in tbr.select(["gt_code", "demand_qty"]).unique().iter_rows(named=True)}
    tag = {r["gt_code"]: r["tt_tl"]
           for r in tbr.select(["gt_code", "tt_tl"]).drop_nulls().unique().iter_rows(named=True)}
    elig: dict[str, set[str]] = {}
    for r in tbr.iter_rows(named=True):
        elig.setdefault(r["gt_code"], set()).add(r["machine"])
    home_m = {r["gt_code"]: r["machine"] for r in
              tbr.filter(pl.col("historical_home")).iter_rows(named=True)}
    volumes = {k: sum(qty[g] for g in tag if tag[g] == k) for k in ("TT", "TL")}
    n_tt = int(round(9 * volumes["TT"] / max(sum(volumes.values()), 1.0)))

    best = None
    best_reach = None
    for combo in combinations(range(1, 10), n_tt):
        tt_set = set(combo)
        group = {n: ("TT" if n in tt_set else "TL") for n in range(1, 10)}
        bad_n = 0
        bad_q = 0.0
        for g, tg in tag.items():
            wanted = {f"TBMTBR{n}Stage2" for n in group if group[n] == tg}
            if not (elig.get(g, set()) & wanted):
                bad_n += 1
                bad_q += qty[g]
        reach = {}
        for n in group:
            machine = f"TBMTBR{n}Stage2"
            reach[n] = sum(qty[g] for g in tag
                           if tag[g] == group[n] and machine in elig.get(g, set()))
        dead = sum(v <= 0 for v in reach.values())
        deficit = 0.0
        for tg in ("TT", "TL"):
            machines = [n for n in group if group[n] == tg]
            fair = volumes[tg] / max(len(machines), 1)
            deficit += sum(max(0.0, fair - reach[n]) for n in machines)
        affinity = 0.0
        for g, machine in home_m.items():
            n = int(machine.replace("TBMTBR", "").replace("Stage2", ""))
            if tag.get(g) == group[n]:
                affinity += qty.get(g, 0.0)
        key = (bad_n, bad_q, dead, round(deficit, 6), -affinity, combo)
        if best is None or key < best:
            best, best_reach = key, reach

    chosen = set(best[-1])
    old_group = pl.read_parquet(paths.wh_derived(f"cap_ttl_groups_{month}.parquet"))
    make = {r["machine"]: r["make"] for r in old_group.iter_rows(named=True)}
    groups = pl.DataFrame([{
        "plant": "TBR", "machine": f"TBMTBR{n}Stage2",
        "make": make.get(f"TBMTBR{n}Stage2", ""),
        "group": "TT" if n in chosen else "TL",
    } for n in range(1, 10)])

    group_map = {r["machine"]: r["group"] for r in groups.iter_rows(named=True)}
    pref = pref.with_columns(
        pl.when(pl.col("plant") == "TBR")
        .then(pl.col("machine").replace_strict(group_map, default=None))
        .otherwise(None).alias("recommended_b16_group"))
    pref = pref.with_columns(
        ((pl.col("plant") != "TBR") |
         (pl.col("tt_tl") == pl.col("recommended_b16_group")))
        .alias("usable_in_recommended_group"))
    pref = pref.with_columns(pl.col("sku_codes").list.join("|").alias("sku_codes"))

    pq = paths.wh_derived(f"machine_gt_preference_{month}.parquet")
    csv = paths.OUTPUT / "analysis" / f"machine_gt_preference_{month}.csv"
    grp_out = paths.wh_derived(f"cap_ttl_groups_history_{month}.parquet")
    pq.parent.mkdir(parents=True, exist_ok=True)
    csv.parent.mkdir(parents=True, exist_ok=True)
    pref.write_parquet(pq)
    pref.write_csv(csv)
    groups.write_parquet(grp_out)

    if a.activate:
        live = paths.wh_derived(f"cap_ttl_groups_{month}.parquet")
        backup = paths.wh_derived(f"cap_ttl_groups_{month}.pre_history.parquet")
        if not backup.exists():
            shutil.copy2(live, backup)
        shutil.copy2(grp_out, live)

    print(f"preference rows: {pref.height} -> {pq}")
    print(f"audit CSV: {csv}")
    print(f"recommended TT machines: {sorted(chosen)}")
    print(f"TBR9 group: {'TT' if 9 in chosen else 'TL'}; reachable demand "
          f"{best_reach[9]:,.0f} tyres")
    print(f"B16 score: uncovered GTs={best[0]}, volume={best[1]:,.0f}, "
          f"dead machines={best[2]}, fair-share deficit={best[3]:,.0f}, "
          f"home-aligned demand={-best[4]:,.0f}")
    print("activated" if a.activate else "recommendation only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
