"""Mine each GT's HOME MACHINE from 8 months of MES.

    PYTHONPATH=. python scripts/build_gt_home_machine.py
    -> INPUT/derived/gt_home_machine.parquet

WHY
  Measured over Dec 2025 - Jul 2026, a PCR GT changes machine between
  consecutive build runs **0 % of the time in seven of eight months** (3 % in
  Dec). TBR is 0-14 %. That is not a tendency, it is a policy: a GT has a home
  machine and stays on it.

  This is FINER than `machine_rim_lock.parquet`. The rim lock says machine M
  runs rim R; several GTs share a rim, and R13 alone has three machines. Home
  machine says WHICH of them a given GT belongs to.

  An earlier per-GT pinning attempt failed (over-constrained at 3.6 GTs per
  machine) -- but that pinned by capacity and eligibility PENALTY, i.e. an
  assignment we invented. This is the plant's own revealed assignment, which is
  feasible by construction because the plant ran it.

COLUMNS
  plant · gt_code · machine · share · tyres_8mo · rank · n_machines
  rank 1 is the home machine; 2+ are the fallbacks the plant itself used, in
  descending volume, so a spill can follow the plant's order rather than ours.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from planner.data.warehouse import duck

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT.parent.parent / "INPUT" / "derived" / "gt_home_machine.parquet"

Q = """
SELECT plant, itemCode AS gt_code, machineCode AS machine, count(*) AS n
FROM v_build
WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
GROUP BY 1, 2, 3
"""


def main() -> None:
    df = pl.DataFrame(
        duck().execute(Q).fetchall(),
        schema=["plant", "gt_code", "machine", "n"], orient="row")
    tot = df.group_by(["plant", "gt_code"]).agg(
        pl.col("n").sum().alias("t"), pl.len().alias("n_machines"))
    df = (df.join(tot, on=["plant", "gt_code"])
          .with_columns((pl.col("n") / pl.col("t")).alias("share"))
          .sort(["plant", "gt_code", "n"], descending=[False, False, True]))
    df = df.with_columns(
        pl.col("n").rank("ordinal", descending=True)
        .over(["plant", "gt_code"]).cast(pl.Int32).alias("rank"))
    out = df.select(["plant", "gt_code", "machine",
                     pl.col("share").round(3),
                     pl.col("n").alias("tyres_8mo"), "rank", "n_machines"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.write_parquet(OUT)

    print("GT HOME MACHINE, mined over 8 months\n")
    for p in ("PCR", "TBR"):
        d = out.filter((pl.col("plant") == p) & (pl.col("rank") == 1))
        if not d.height:
            continue
        s = np.array(d["share"], float)
        allm = out.filter(pl.col("plant") == p)
        print(f"  {p}: {d.height} GTs · home-machine share p50 {np.median(s)*100:.0f}% "
              f"· mean {s.mean()*100:.0f}%")
        print(f"      GTs >=90% on one machine: {(s >= 0.9).sum()}/{len(s)} "
              f"· >=70%: {(s >= 0.7).sum()} · <50%: {(s < 0.5).sum()}")
        print(f"      machines a GT ever used: p50 {float(allm.group_by('gt_code').len()['len'].median()):.0f}")
        # how many GTs land on each machine -- the feasibility question
        load = d.group_by("machine").len().sort("len", descending=True)
        print(f"      GTs per home machine: max {int(load['len'].max())} "
              f"· p50 {float(load['len'].median()):.0f} over {load.height} machines")
    print(f"\n  -> {OUT}")


if __name__ == "__main__":
    main()
