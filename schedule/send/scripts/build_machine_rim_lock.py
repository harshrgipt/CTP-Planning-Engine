"""Mine the plant's machine -> rim LOCK from 8 months of MES.

    PYTHONPATH=. python scripts/build_machine_rim_lock.py
    -> INPUT/derived/machine_rim_lock.parquet

WHY THIS FILE EXISTS
  The engine plans PCR machine eligibility from an INCH-RANGE CAPABILITY
  statement (`l2_capability.PCR_INCH`: machines 1-2 hold 12-20 inch, 3-5 hold
  12-16, 6-11 hold 13-18). Rims 13-16 therefore match ALL ELEVEN machines, so a
  GT gets 9.75 options and the option set has a rim purity of 20%. No scheduler
  can produce the plant's 92% same-size share from a 20%-pure option set --
  the rim-clustered choices do not exist to pick.

  Capability is not policy. Over 8 months the plant locks each machine to ONE
  rim and holds it: purity 66-100%, median ~95%. That lock is a fact in the data
  and does not need to be solved for.

WHAT IT REPRODUCES
  The mined lock matches the load arithmetic exactly. PCR needs
  R13 3.0 · R15 1.4 · R18 1.2 · R12 1.1 · R17 1.0 · R14 0.9 · R16 0.8
  = 9.4 machine-equivalents, and the plant assigns 3+2+2+1+1+1+1 = 11 machines,
  absorbing the rounding on TBMPCR2 -- the one genuinely mixed machine at 66%.

TIERS, emitted so the consumer can book them in the plant's own order:
  hard    purity >= 99.5%   locked; no other rim may take it
  primary 85-99.5%          own rim first, spill permitted
  flex    < 85%             serves its own rim AND absorbs other rims' tails

Read-only against the warehouse. Writes one parquet.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from planner.data.warehouse import duck

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT.parent.parent / "INPUT" / "derived" / "machine_rim_lock.parquet"

Q = """
SELECT plant, machineCode AS machine, itemCode AS gt, count(*) AS n
FROM v_build
WHERE stage = 2 AND itemCode IS NOT NULL AND machineCode IS NOT NULL
GROUP BY 1, 2, 3
"""


def main() -> None:
    sz = pl.read_parquet(ROOT.parent.parent / "INPUT" / "derived" / "gt_size.parquet")
    rim = {r["gt_code"]: str(r["rim"]) for r in sz.iter_rows(named=True)
           if r.get("gt_code") and r.get("rim")}
    rows = duck().execute(Q).fetchall()

    per: dict[tuple, dict] = {}
    for plant, machine, gt, n in rows:
        rr = rim.get(gt)
        if rr:
            per.setdefault((plant, machine), {})
            per[(plant, machine)][rr] = per[(plant, machine)].get(rr, 0) + n

    out = []
    for (plant, machine), d in per.items():
        tot = sum(d.values())
        dom = max(d, key=d.get)
        purity = 100.0 * d[dom] / tot
        out.append({
            "plant": plant, "machine": machine, "locked_rim": dom,
            "purity": round(purity, 1), "tyres_8mo": tot,
            "n_rims_seen": len(d),
            "tier": "hard" if purity >= 99.5 else
                    ("primary" if purity >= 85.0 else "flex"),
        })

    df = (pl.DataFrame(out)
          .sort(["plant", "locked_rim", "tyres_8mo"], descending=[False, False, True]))
    # rank 1 = a rim's primary machine, 2+ = its additional machines
    df = df.with_columns(
        pl.col("tyres_8mo").rank("ordinal", descending=True)
        .over(["plant", "locked_rim"]).cast(pl.Int32).alias("rank"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT)

    pl.Config.set_tbl_rows(40)
    print(df)
    print()
    for p in ("PCR", "TBR"):
        d = df.filter(pl.col("plant") == p)
        if not d.height:
            continue
        t = d.group_by("tier").len().sort("tier")
        print(f"  {p}: {d.height} machines · {d['locked_rim'].n_unique()} rims · "
              f"purity p50 {float(d['purity'].median()):.0f}% · "
              + " ".join(f"{r['tier']}:{r['len']}" for r in t.iter_rows(named=True)))
    print(f"\n  -> {OUT}")


if __name__ == "__main__":
    main()
