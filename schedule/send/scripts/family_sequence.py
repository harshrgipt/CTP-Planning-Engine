"""Family sequencing on a generated plan. Reports before/after; --apply writes.

    python scripts/family_sequence.py runs/july_v3 [--apply]

Re-orders lots WITHIN each (machine, day) by construction distance. Same lots,
same machines, same daily quantities -- only intra-day order changes, so daily
supply to the presses is untouched and the FIFO build/cure pairing per GT is
preserved. That is deliberate: an earlier EDD attempt re-ordered ACROSS days,
which changed the pairing that produced its own deadlines and regressed
inventory 7,041 -> 10,028.

Also prints the theta sweep (setup minutes vs family count) so the cut is
chosen from the curve rather than guessed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl

from planner.learn.construction import (COMPONENTS, distance_fn, families,
                                        learn_weights, order_atsp, signatures)

ROOT = Path(__file__).resolve().parent.parent


def main(run: Path, apply: bool = False) -> None:
    sig = signatures(ROOT / "warehouse" / "derived" / "sku_construction.parquet")
    w = learn_weights(sig, "TBR")
    if not w:
        print("  no weights learned"); return

    print("=" * 74)
    print("1. LEARNED COMPONENT WEIGHTS (NNLS on MES gaps, minutes)")
    print("=" * 74)
    print(f"  n = {w['n']:,} changeovers   R2 = {w['r2']}   "
          f"intercept = {w['intercept_min']:.1f} min")
    for c, v in sorted(w["weights_min"].items(), key=lambda t: -t[1]):
        flag = "  (unidentifiable)" if c in w["unidentifiable"] else ""
        print(f"    {c:<22} {v:>6.2f} min   "
              f"[{w['obs_varying'][c]:>5,} obs]{flag}")

    d = distance_fn(sig, w)
    b = pl.read_parquet(run / "build_schedule.parquet").filter(
        pl.col("plant") == "TBR")
    gts = sorted(set(b["gt_code"].to_list()) & set(sig))

    print("\n" + "=" * 74)
    print("2. THETA SWEEP -- families vs achievable intra-family cost")
    print("=" * 74)
    print(f"  {'theta (min)':>12}{'families':>10}{'largest':>9}{'singletons':>12}")
    for th in [3, 5, 8, 10, 12, 15, 20, 25]:
        fam = families(gts, d, th)
        sizes: dict[int, int] = {}
        for g in fam:
            sizes[fam[g]] = sizes.get(fam[g], 0) + 1
        print(f"  {th:>12}{len(sizes):>10}{max(sizes.values()):>9}"
              f"{sum(1 for v in sizes.values() if v == 1):>12}")

    # ---- 3. sequence within (machine, day) ------------------------------
    bd = b.with_columns(pl.col("start_ts").dt.date().alias("_d"))

    def cost_of(df: pl.DataFrame) -> tuple[float, int, int]:
        tot, n, major = 0.0, 0, 0
        s = df.sort(["machine", "start_ts"])
        m = s["machine"].to_list(); g = s["gt_code"].to_list()
        for i in range(1, len(m)):
            if m[i] != m[i - 1] or g[i] == g[i - 1]:
                continue
            v = d(g[i - 1], g[i])
            if v is None:
                continue
            tot += v; n += 1
            if v >= 10:
                major += 1
        return tot, n, major

    before = cost_of(b)
    rows = bd.to_dicts()
    by: dict[tuple, list] = {}
    for r in rows:
        by.setdefault((r["machine"], r["_d"]), []).append(r)
    out = []
    for k in sorted(by, key=lambda t: (t[0], t[1])):
        grp = sorted(by[k], key=lambda r: (r["start_ts"], r["lot_id"]))
        uniq = sorted({r["gt_code"] for r in grp})
        if len(uniq) > 2:
            seq = order_atsp(uniq, d, start=grp[0]["gt_code"])
            rank = {g: i for i, g in enumerate(seq)}
            grp = sorted(grp, key=lambda r: (rank.get(r["gt_code"], 99),
                                             r["start_ts"], r["lot_id"]))
        # re-lay the same time slots in the new order
        slots = sorted([(r["start_ts"], r["end_ts"]) for r in by[k]])
        for r, (s0, e0) in zip(grp, slots):
            out.append({**r, "start_ts": s0, "end_ts": e0})
    nb = pl.DataFrame(out).drop("_d")
    after = cost_of(nb)

    print("\n" + "=" * 74)
    print("3. RESULT -- intra-day resequencing by construction distance")
    print("=" * 74)
    print(f"  {'':<22}{'before':>12}{'after':>12}{'change':>12}")
    print(f"  {'total setup (min)':<22}{before[0]:>12,.0f}{after[0]:>12,.0f}"
          f"{after[0]-before[0]:>+12,.0f}")
    print(f"  {'changeovers':<22}{before[1]:>12,}{after[1]:>12,}"
          f"{after[1]-before[1]:>+12,}")
    print(f"  {'major (>=10 min)':<22}{before[2]:>12,}{after[2]:>12,}"
          f"{after[2]-before[2]:>+12,}")
    print(f"  {'mean min/changeover':<22}{before[0]/max(before[1],1):>12.1f}"
          f"{after[0]/max(after[1],1):>12.1f}"
          f"{after[0]/max(after[1],1)-before[0]/max(before[1],1):>+12.1f}")

    if apply:
        full = pl.read_parquet(run / "build_schedule.parquet")
        keep = full.filter(pl.col("plant") != "TBR")
        pl.concat([keep, nb.select(full.columns)], how="vertical").sort(
            ["plant", "machine", "start_ts", "lot_id"]).write_parquet(
            run / "build_schedule.parquet", compression="zstd")
        (run / "construction_weights.json").write_text(json.dumps(w, indent=2))
        print(f"\n  applied -> {run}/build_schedule.parquet")


if __name__ == "__main__":
    main(Path(sys.argv[1]), "--apply" in sys.argv)
